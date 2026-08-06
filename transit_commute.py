"""
transit_commute.py — door-to-door public-transit commute from each listing to the
profile's work address, via the Google Routes API.

This is the transit counterpart to transit_times.py (which does cycling times to
a station). It answers the question people actually ask — "how long does it take
me to get to work on Muni/BART?" — rather than a proxy for it.

Why Google Routes and not OpenRouteService: ORS has no public-transit profile on
the hosted free tier, only driving/cycling/walking. Routes API returns scheduled
door-to-door itineraries plus, per transit leg, the `headway` — the expected gap
between departures. That headway is what makes the frequency ranking possible
without hand-maintaining a table of Muni timetables.

Set GOOGLE_MAPS_API_KEY in secrets.env to switch this on. With no key the module
is inert: it returns {} and everything downstream degrades to "no commute data",
exactly like bike times do without ORS_API_KEY.

Scoring
-------
Three things decide whether transit at an address is actually good, and the
profile weights them (see [commute] in the TOML):

    time         door-to-door minutes against the profile's max_minutes
    frequency    the WORST headway on the itinerary — a 4-minute BART leg does
                 not rescue a 30-minute bus leg, so the bottleneck is the number
                 that describes the trip
    redundancy   how many genuinely different ways there are to make the trip,
                 counted as distinct transit lines across all returned
                 itineraries. One line means one breakdown away from stranded

Each is normalised to 0–100 and combined into `transit_score`.

Rate limiting / cost
--------------------
Results are cached on disk by listing URL and the destination never moves, so
each listing costs exactly one request, once, ever. `defer_on_limit=True` (used
by the scraper) stops early and leaves the rest for the next run; the digest
path uses False so every listing in the email has a commute on it.

Three separate things bound the spend, and they do different jobs:

    _is_included        which listings are worth a call at all — the profile's
                        include list, plus _EXTRA_HOODS on a shortlist gate
    _reserve_slot       requests per minute, so the Pi's cron slot isn't hogged
    _MONTHLY_CALL_CAP   a hard ceiling per calendar month, tracked in a ledger
                        beside the cache. This one is a backstop against a
                        runaway, not a throttle on normal use

Hitting the monthly cap is not a deferral: those listings get no commute until
the month rolls over. It prints loudly when it happens.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import (
    GOOGLE_MAPS_API_KEY,
    COMMUTE_DESTINATION,
    COMMUTE_DESTINATION_NAME,
    COMMUTE_ARRIVE_BY,
    COMMUTE_MAX_MINUTES,
    COMMUTE_WEIGHTS,
    COMMUTE_CACHE_PATH,
    INCLUDE_NEIGHBORHOODS,
    digest_max_price,
)

_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Only the fields the scoring below actually reads. Routes API bills on the
# field mask, and an over-broad mask is both slower and more expensive.
_FIELD_MASK = ",".join([
    "routes.duration",
    # Geometry for drawing the trip on the dashboard map. The request already
    # asks for transitDetails, so this does not move the call into a higher
    # billing tier -- worth re-checking against current Routes API pricing if
    # the bill ever looks wrong.
    "routes.polyline.encodedPolyline",
    # Per-step geometry, so the map can colour each leg by mode instead of
    # drawing one undifferentiated line for the whole trip.
    "routes.legs.steps.polyline.encodedPolyline",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.transitDetails.headway",
    "routes.legs.steps.transitDetails.transitLine.name",
    "routes.legs.steps.transitDetails.transitLine.nameShort",
    "routes.legs.steps.transitDetails.transitLine.vehicle.type",
    "routes.legs.steps.transitDetails.stopDetails.departureStop.name",
])

# Routes API allows far more than this, but the Pi runs this inside a 10-minute
# cron slot alongside the scraper, so it stays deliberately gentle.
_MAX_PER_MIN = 60
_call_times = []

# Headway bounds for the frequency score. 5 min or better is "just turn up and
# go"; 30 min or worse means the timetable runs your day.
_HEADWAY_BEST_MIN  = 5
_HEADWAY_WORST_MIN = 30

# Distinct lines across all itineraries needed to score full marks on redundancy.
_REDUNDANCY_TARGET = 4

# Consecutive request failures before assuming the problem is configuration
# rather than one bad address, and stopping.
_MAX_CONSECUTIVE_FAILURES = 5


# ── Monthly spend ceiling ─────────────────────────────────────────────────────
#
# Every result is cached forever and the destination never moves, so a listing
# costs exactly one call once. Steady-state demand is therefore one call per new
# eligible listing: measured over 2026-08-04, that was 24 listings/day, ~750 a
# month. This cap sits ~3x above that and is not meant to bind in normal running.
#
# What it is for is the failure mode where something starts re-requesting and
# nobody notices until the bill arrives — a profile flipped to `filter = false`,
# a lost or corrupted cache file, a scraper change that re-keys listing URLs so
# every row looks new. Any of those turns a 750/month job into a per-run one.
#
# Runaway protection, not budget management.
#
# CONFIRMED on the billing report 2026-08-05: these requests bill as
# "Routes: Compute Routes Essentials", SKU 9EFF-679A-9B16, whose free allowance
# is 10,000 calls/month. 198 calls that month, $0.00. That settles what the code
# could only infer -- travelMode TRANSIT with no routingPreference, no tollInfo
# and no intermediate waypoints does not escalate past Essentials.
#
# Measured burn is ~63 eligible listings/day, ~1,890/month, which is 19% of the
# allowance. The cap sits at 3,000 rather than just above that: the burn figure
# rests on a single clean day of data and August is peak listing season, so the
# honest uncertainty band tops out near 2,650 and a tighter cap would bind on a
# busy month for no reason. At 3,000 it is still under a third of the free tier.
#
# What it is actually for is the failure mode where something starts
# re-requesting -- a lost cache, a scraper change that re-keys listing URLs, a
# profile flipped to `filter = false`. The scraper runs every 10 minutes against
# ~300 active listings, so a runaway would burn the entire free allowance in
# hours. 3,000 stops that inside about one hour.
#
# Per machine: the Pi and a laptop keep separate ledgers, so the true ceiling is
# a multiple of this. For a project-wide guarantee set a quota cap on the Routes
# API in the Cloud console, which Google enforces across every caller of the key.
_MONTHLY_CALL_CAP = 3000

_BUDGET_PATH = Path(COMMUTE_CACHE_PATH).with_name(
    Path(COMMUTE_CACHE_PATH).stem + "_budget.json"
)


def _budget_month() -> str:
    """Calendar month key, in the profile's own timezone rather than UTC."""
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")


def _budget_load() -> dict:
    """{'month': 'YYYY-MM', 'calls': int}, reset when the month rolls over."""
    month = _budget_month()
    try:
        with open(_BUDGET_PATH) as f:
            data = json.load(f)
        if data.get("month") == month:
            return {"month": month, "calls": int(data.get("calls", 0))}
    except (FileNotFoundError, ValueError, TypeError):
        pass
    return {"month": month, "calls": 0}


def _budget_charge(state: dict, n: int = 1) -> None:
    """Record n spent calls. Written through on every call, not at the end.

    Buffering this until the run finished would lose the count on exactly the
    runs that matter — a crash mid-backfill, or the Pi's cron slot killing a
    long run — and a spend ledger that forgets what it spent is worse than none.
    """
    state["calls"] += n
    try:
        with open(_BUDGET_PATH, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"  Commute: could not write call ledger ({e}) — "
              f"continuing, but the monthly cap is not being enforced")

# Rail is materially more reliable than a bus in traffic, so an all-rail trip
# gets a small bump inside the frequency term.
_RAIL_VEHICLES = {
    "HEAVY_RAIL", "SUBWAY", "METRO_RAIL", "LIGHT_RAIL", "RAIL",
    "COMMUTER_TRAIN", "HIGH_SPEED_TRAIN", "LONG_DISTANCE_TRAIN", "MONORAIL", "TRAM",
}


# Routes API calls are billed individually, and a `filter = false` profile keeps
# every listing scraped — the whole subarea, not just the neighborhoods someone
# asked about. Restricting calls to the include list is what keeps the bill
# proportional to the search rather than to the city.
_INCLUDED_HOODS = {h.strip() for h in INCLUDE_NEIGHBORHOODS if h.strip()}

# Neighborhoods that earn a commute call despite not being in the include list,
# and the gate a listing there has to clear to earn one.
#
# "Way Out There" is the deliberately vague shape covering western SF — the
# Sunset and out past it. Nobody puts it in an include list, so nothing in it
# was ever measured. That was invisible rather than harmless: the map's commute
# filter treats an unmeasured listing as passing (see the null handling in
# analyze_listings' filter script), so selecting "under 30 min" left every
# unmeasured Outer Sunset dot on screen looking like it qualified. The only
# nearby trip that *was* measured is an Inner Sunset listing at 31 minutes, and
# the Outer Sunset is a couple of km further west again.
#
# The gate is tight rather than "just measure everything out there": these are
# billed calls, and a 1BR at $4,400 in the Outer Sunset is not a listing this
# search would act on. A 2BR inside the digest ceiling is — that's the shortlist
# the dashboard map now draws routes for, so those are the numbers worth buying.
_EXTRA_HOODS        = {"Way Out There"}
_EXTRA_MIN_BEDROOMS = 2
_EXTRA_MAX_PRICE    = digest_max_price or 4000

# Every listing at or above this many bedrooms earns a call regardless of where
# it is, and regardless of price. This supersedes the _EXTRA_HOODS gate above
# for anything 2BR+ — that gate now only really matters as documentation of why
# the west side started getting measured at all.
#
# The reason is that neighborhood is the wrong axis for the scarce thing. 1BRs
# are 3/4 of what gets scraped and are everywhere; 2BRs are ~32/day citywide and
# are the listings this search is actually deciding between, so measuring all of
# them and none of the distant 1BRs is a better use of the same budget than
# measuring everything inside a polygon list.
#
# It also closes a gap that was invisible: "Rest of City" is not a place, it is
# the bucket for listings matching no drawn polygon, and it held 17 shortlist
# candidates with no commute — one of them 400m from the destination.
#
# Set to None to turn this off and fall back to include-list + _EXTRA_HOODS.
_ALWAYS_ROUTE_MIN_BEDROOMS = 2


def _num(value):
    """CSV-safe float, or None for blanks/NaN/non-numeric."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(v) else v


def _hoods_of(listing) -> set[str]:
    return {
        h.strip()
        for h in str(listing.get("neighborhoods") or "").split(",")
        if h.strip()
    }


def _is_included(listing) -> bool:
    """True if this listing is worth spending a Routes API call on.

    An empty include list means "everywhere", matching how the digest and the
    dashboard already read it. Otherwise: anything in a named neighborhood, plus
    anything in _EXTRA_HOODS that clears the shortlist gate above.
    """
    if not _INCLUDED_HOODS:
        return True

    # Bedrooms first — this one does not consult the neighborhood at all.
    if _ALWAYS_ROUTE_MIN_BEDROOMS is not None:
        beds = _num(listing.get("num_bedrooms"))
        if beds is not None and beds >= _ALWAYS_ROUTE_MIN_BEDROOMS:
            return True

    hoods = _hoods_of(listing)
    if hoods & _INCLUDED_HOODS:
        return True

    if hoods & _EXTRA_HOODS:
        beds  = _num(listing.get("num_bedrooms"))
        price = _num(listing.get("price"))
        # Unknown beds or price fails the gate. Out here the listing only earns
        # a call by demonstrably being a shortlist candidate, and a missing
        # field can't demonstrate anything.
        return (
            beds is not None and beds >= _EXTRA_MIN_BEDROOMS
            and price is not None and price <= _EXTRA_MAX_PRICE
        )

    return False


def _reserve_slot(defer_on_limit: bool) -> bool:
    """Keep requests under _MAX_PER_MIN. Mirrors transit_times._reserve_ors_slots."""
    now = time.time()
    _call_times[:] = [t for t in _call_times if t > now - 60]
    if len(_call_times) < _MAX_PER_MIN:
        return True
    if defer_on_limit:
        return False
    wait = 60 - (now - _call_times[0]) + 0.5
    print(f"    Commute rate limit approaching, sleeping {wait:.0f}s…")
    time.sleep(wait)
    _call_times[:] = [t for t in _call_times if t > time.time() - 60]
    return True


def _next_arrival_time() -> str:
    """RFC3339 UTC for the next weekday at the profile's arrive-by time.

    Transit itineraries are only meaningful against a clock — a 9am Tuesday trip
    and a 3am Sunday trip on the same route are different journeys. Pinning every
    listing to the same weekday-morning slot is what makes their commute numbers
    comparable to each other.
    """
    tz    = ZoneInfo("America/Los_Angeles")
    hour, minute = COMMUTE_ARRIVE_BY
    local = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)

    # Always look forward: a time earlier today is already gone, and the API
    # rejects arrival times in the past.
    if local <= datetime.now(tz) + timedelta(minutes=30):
        local += timedelta(days=1)
    while local.weekday() >= 5:            # 5 = Sat, 6 = Sun
        local += timedelta(days=1)

    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(value) -> float:
    """Routes API durations are strings like '1345s'."""
    if not value:
        return 0.0
    try:
        return float(str(value).rstrip("s"))
    except ValueError:
        return 0.0


# Vehicle type is enough to tell SF's three modes apart, confirmed against live
# results: Muni Metro comes back TRAM (J/K/M/N), BART comes back SUBWAY
# (Yellow-N and friends), and anything on rubber comes back BUS. A mapping
# rather than an if-chain so an unfamiliar type lands on a neutral colour
# instead of being confidently mislabelled as one of the three.
_MODE_BY_VEHICLE = {
    "BUS":                 "bus",
    "TROLLEYBUS":          "bus",
    "INTERCITY_BUS":       "bus",
    "TRAM":                "muni",
    "LIGHT_RAIL":          "muni",
    "MONORAIL":            "muni",
    "SUBWAY":              "bart",
    "METRO_RAIL":          "bart",
    "HEAVY_RAIL":          "bart",
    "COMMUTER_TRAIN":      "rail",   # Caltrain — not BART, worth its own colour
    "HIGH_SPEED_TRAIN":    "rail",
    "LONG_DISTANCE_TRAIN": "rail",
}


def _decode_polyline(encoded: str) -> list[list[float]]:
    """Google's encoded polyline -> [[lat, lon], ...], the order folium wants.

    Implemented here rather than pulled in as a dependency: it is the standard
    algorithm, it is twenty lines, and the Pi's conda env is slow to change.
    """
    if not encoded:
        return []
    coords, index, lat, lng = [], 0, 0, 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            result = shift = 0
            while index < length:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift  += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        coords.append([lat / 1e5, lng / 1e5])
    return coords


def _summarise_route(route: dict) -> dict:
    """Pull minutes, walking, and per-leg transit details out of one itinerary."""
    total_min = _seconds(route.get("duration")) / 60
    walk_sec, legs, segments = 0.0, [], []

    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            step_geom = _decode_polyline(
                (step.get("polyline") or {}).get("encodedPolyline") or ""
            )

            if step.get("travelMode") == "WALK":
                walk_sec += _seconds(step.get("staticDuration"))
                if step_geom:
                    segments.append({"mode": "walk", "geometry": step_geom})
                continue

            details = step.get("transitDetails") or {}
            line    = details.get("transitLine") or {}
            stop    = ((details.get("stopDetails") or {}).get("departureStop") or {})
            vehicle = ((line.get("vehicle") or {}).get("type") or "").upper()
            legs.append({
                "line":    line.get("nameShort") or line.get("name") or "?",
                "vehicle": vehicle,
                "headway": _seconds(details.get("headway")) / 60 or None,
                "stop":    stop.get("name") or "",
            })
            if step_geom:
                segments.append({
                    "mode":     _MODE_BY_VEHICLE.get(vehicle, "other"),
                    "geometry": step_geom,
                })

    return {
        "minutes":      int(round(total_min)),
        "walk_minutes": int(round(walk_sec / 60)),
        "legs":         legs,
        "segments":     segments,
        # Whole-trip geometry is kept as a fallback: if a response ever omits
        # step polylines, the map can still draw the route as one line rather
        # than showing nothing.
        "geometry":     _decode_polyline(
            (route.get("polyline") or {}).get("encodedPolyline") or ""
        ),
    }


def _score(best: dict, alternatives: list[dict]) -> dict:
    """Blend commute time, frequency, and redundancy into a 0–100 score."""
    # ── time ──
    # Full marks at half the budget or better, zero at the budget. Beyond it the
    # listing is out of scope anyway, so there's nothing to gain from grading it.
    floor = COMMUTE_MAX_MINUTES / 2
    if best["minutes"] <= floor:
        time_score = 100.0
    elif best["minutes"] >= COMMUTE_MAX_MINUTES:
        time_score = 0.0
    else:
        span = COMMUTE_MAX_MINUTES - floor
        time_score = 100.0 * (COMMUTE_MAX_MINUTES - best["minutes"]) / span

    # ── frequency ──
    # The bottleneck leg, not the average: you wait for the worst one.
    headways = [l["headway"] for l in best["legs"] if l["headway"]]
    worst    = max(headways) if headways else None
    if worst is None:
        # A walk-only trip has no headway to wait for. That's the best possible
        # case, not a missing value.
        freq_score = 100.0 if not best["legs"] else 50.0
    elif worst <= _HEADWAY_BEST_MIN:
        freq_score = 100.0
    elif worst >= _HEADWAY_WORST_MIN:
        freq_score = 0.0
    else:
        span = _HEADWAY_WORST_MIN - _HEADWAY_BEST_MIN
        freq_score = 100.0 * (_HEADWAY_WORST_MIN - worst) / span

    if best["legs"] and all(l["vehicle"] in _RAIL_VEHICLES for l in best["legs"]):
        freq_score = min(100.0, freq_score + 10.0)

    # ── redundancy ──
    # Distinct lines across every itinerary Google returned. Two routes that both
    # ride the N Judah are one line's worth of resilience, not two.
    lines = {l["line"] for route in [best, *alternatives] for l in route["legs"]}
    redundancy_score = 100.0 * min(len(lines), _REDUNDANCY_TARGET) / _REDUNDANCY_TARGET

    w     = COMMUTE_WEIGHTS
    total = w["time"] + w["frequency"] + w["redundancy"]
    score = (
        w["time"]       * time_score
        + w["frequency"]  * freq_score
        + w["redundancy"] * redundancy_score
    ) / total

    return {
        "transit_score":    int(round(score)),
        "worst_headway":    round(worst, 1) if worst else None,
        "distinct_lines":   len(lines),
        "lines":            sorted(lines),
    }


_reported_errors: set[str] = set()


def _report_once(message: str) -> None:
    """Print a failure the first time only.

    A misconfigured key fails identically for every listing. Printing it once per
    listing buries the run's real output in hundreds of identical lines and makes
    a config problem look like a data problem.
    """
    if message in _reported_errors:
        return
    _reported_errors.add(message)
    print(f"  Commute: {message}")


def _request(lat: float, lon: float, arrival: str, timeout: int = 20) -> list[dict] | None:
    """One Routes API call. Returns the raw route list, or None on any failure."""
    body = {
        "origin":      {"location": {"latLng": {"latitude": lat, "longitude": lon}}},
        "destination": {"location": {"latLng": {"latitude":  COMMUTE_DESTINATION[1],
                                                "longitude": COMMUTE_DESTINATION[0]}}},
        "travelMode":  "TRANSIT",
        "arrivalTime": arrival,
        "computeAlternativeRoutes": True,
        "languageCode": "en-US",
    }
    headers = {
        "Content-Type":     "application/json",
        "X-Goog-Api-Key":   GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": _FIELD_MASK,
    }

    try:
        resp = requests.post(_ENDPOINT, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        _report_once(f"request failed ({type(e).__name__}: {e})")
        return None

    if resp.status_code != 200:
        # The API puts the useful part in error.message; the rest is noise.
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:200])
        except ValueError:
            detail = resp.text[:200]
        _report_once(f"HTTP {resp.status_code} — {detail}")
        return None

    try:
        return resp.json().get("routes", [])
    except ValueError:
        _report_once("response was not JSON")
        return None


def compute_commutes(listings, defer_on_limit: bool = False) -> dict:
    """Door-to-door transit commute for each listing, keyed by URL.

    Each value is:
        {'minutes', 'walk_minutes', 'transit_score', 'worst_headway',
         'distinct_lines', 'lines', 'summary'}

    Listings already in the cache, or without coordinates, cost nothing. When the
    per-minute budget runs out and defer_on_limit is set, the remainder is left
    for the next run — the cache is on disk, so no work is repeated.
    """
    if not GOOGLE_MAPS_API_KEY:
        print("  Commute: no GOOGLE_MAPS_API_KEY set — skipping transit times")
        return {}
    if not COMMUTE_DESTINATION:
        print("  Commute: no [commute] destination in profile — skipping transit times")
        return {}

    try:
        with open(COMMUTE_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, ValueError):
        cache = {}

    arrival      = _next_arrival_time()
    result       = {}
    cache_dirty  = False
    deferred     = 0
    out_of_area  = 0
    capped       = 0
    consecutive_failures = 0
    budget       = _budget_load()

    for i, pt in enumerate(listings):
        url = pt.get("url")
        if not url:
            continue

        # "segments" is required as well as "minutes" so that entries cached
        # before per-step geometry was requested get refetched once, rather than
        # sitting there forever as a commute time the map can only draw as one
        # undifferentiated line. The test is key presence, not truthiness: a
        # trip the API returns no polylines for caches an empty list and must
        # not be retried every run.
        cached = cache.get(url)
        if cached and "minutes" in cached and "segments" in cached:
            result[url] = cached
            continue

        # Checked after the cache, so a listing that already has a commute time
        # keeps showing it even if the include list later moves away from it.
        # Only *new* calls are withheld.
        if not _is_included(pt):
            out_of_area += 1
            continue

        lon, lat = pt.get("lon"), pt.get("lat")
        if not (pd.notna(lon) and pd.notna(lat)):
            continue

        # Checked before the rate limiter, not after: _reserve_slot may sleep,
        # and there is no point waiting out a minute for a slot we are then
        # forbidden to spend. Unlike the rate limit this is not a deferral —
        # the month has to turn over before these get another chance.
        #
        # `continue`, not `break`. Breaking skipped every remaining listing
        # including the already-cached ones, whose `result[url] = cached` sits
        # above this — so once the cap tripped, compute_commutes returned a dict
        # missing commutes it had already paid for. The digest builds its
        # over-budget filter from that dict, so a capped month would have
        # quietly changed which listings made someone's email. Cached entries
        # cost nothing; only new calls are withheld.
        if budget["calls"] >= _MONTHLY_CALL_CAP:
            capped += 1
            continue

        if not _reserve_slot(defer_on_limit):
            deferred = sum(
                1 for p in listings[i:] if p.get("url") and p["url"] not in cache
            )
            break

        _call_times.append(time.time())
        # Charged before the response, so a request that fails or times out
        # still counts. It reached Google either way, and a ledger that only
        # records successes is not a spend ledger.
        _budget_charge(budget)
        routes = _request(float(lat), float(lon), arrival)

        if routes is None:
            # A bad key, a disabled API, or a billing problem fails identically
            # for every listing. Give up after a few rather than working through
            # hundreds of requests that cannot succeed.
            consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                print(f"  Commute: giving up after {consecutive_failures} "
                      f"consecutive failures — fix the error above and re-run. "
                      f"Nothing else is affected.")
                break
            continue

        consecutive_failures = 0
        if not routes:
            # Empty list (rather than None) means the API worked and simply
            # found no itinerary — a real answer about this address.
            print(f"  Commute: no transit route for {url[-30:]}")
            continue

        summaries = [_summarise_route(r) for r in routes]
        best      = min(summaries, key=lambda s: s["minutes"])
        others    = [s for s in summaries if s is not best]

        info = {**best, **_score(best, others)}
        info["summary"] = _describe(info)

        result[url] = info
        cache[url]  = info
        cache_dirty = True
        print(f"  Commute: {url[-30:]} → {info['minutes']} min "
              f"(score {info['transit_score']}, {info['distinct_lines']} line(s))")

    if out_of_area:
        print(f"  Commute: skipped {out_of_area} listing(s) outside the "
              f"profile's neighborhoods — no API call made")

    if deferred:
        print(f"  Commute: rate limit hit — deferred {deferred} listing(s) to next run")

    if capped:
        print(f"  Commute: monthly call cap reached "
              f"({budget['calls']}/{_MONTHLY_CALL_CAP} for {budget['month']}) — "
              f"skipped {capped} listing(s). Nothing else is affected; the cap "
              f"resets next month. Raise _MONTHLY_CALL_CAP if this is expected.")

    if cache_dirty:
        try:
            with open(COMMUTE_CACHE_PATH, "w") as f:
                json.dump(cache, f)
        except OSError as e:
            print(f"  Commute: could not write cache ({e})")

    return result


def _describe(info: dict) -> str:
    """One-line human summary, e.g. '32 min to work, 7 min walk to transit'.

    Deliberately does not name lines. `lines` is the redundancy set — every
    distinct line across the best itinerary *and* all its alternatives, sorted
    alphabetically — so the first few are often lines the trip never touches:
    one cached trip advertised "49, 7, J" while actually riding nothing but the
    N. Printed next to a walk time it read as "walk to the 49", which was
    simply untrue. The count still feeds transit_score; it just no longer gets
    spelled out as though it were an itinerary.
    """
    bits = [f"{info['minutes']} min to {COMMUTE_DESTINATION_NAME}"]
    if info.get("walk_minutes"):
        bits.append(f"{info['walk_minutes']} min walk to transit")
    if info.get("worst_headway"):
        bits.append(f"every ~{info['worst_headway']:.0f} min")
    return ", ".join(bits)


# ── Backfill CLI ──────────────────────────────────────────────────────────────
#
# compute_commutes() already refetches any entry missing "segments", but nothing
# ever offers it the listings that need it: the scraper passes only *new*
# records and the digest only the listings going in that email. An entry cached
# before per-step geometry was requested therefore stays stale forever, and the
# map draws it as one undifferentiated "Other transit" line -- which is how 32
# of 49 cached trips ended up grey despite being ordinary bus/Muni/BART rides.
#
# This walks the listing data instead, so those entries get seen exactly once.
# Complete entries cost nothing: the cache check skips them before any request
# is made, so the bill is one call per listing that genuinely needs one.
#
# It covers two populations, and both matter:
#
#   stale    cached before per-step geometry was requested
#   missing  eligible but never cached at all — the case that appears whenever
#            _is_included widens (adding _EXTRA_HOODS did exactly this) and the
#            case that matters most in deployment, because the cache is
#            gitignored. The Pi and a laptop each keep their own, so a backfill
#            run in one place does not reach the other, and the Pi is what
#            builds the published dashboard. Run this there after widening.
#
# Active listings only for the "missing" set: a removed listing is off the map
# and out of the digest, so a call spent on one buys nothing. Stale entries are
# still refetched from active + archive, since those are already paid for.
#
#     python transit_commute.py --dry-run     # count them, spend nothing
#     python transit_commute.py               # fetch
def _main() -> None:
    import argparse

    from config import DATA_ACTIVE, DATA_ARCHIVE, add_profile_arg, PROFILE_NAME

    parser = argparse.ArgumentParser(
        description="Fill in commutes that are stale or were never fetched."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be fetched; make no API calls.")
    add_profile_arg(parser)
    args = parser.parse_args()

    # Checked before any counting. compute_commutes() bails on these too, so a
    # real run was always safe — but the dry run happily reported "would spend
    # up to 64" for a profile that has nothing to route to, which is exactly the
    # number someone deciding whether they can afford this would misread.
    if not COMMUTE_DESTINATION:
        print(f"Profile {PROFILE_NAME} has no [commute] destination — "
              f"nothing to fetch, nothing to spend.")
        return
    if not GOOGLE_MAPS_API_KEY:
        print("No GOOGLE_MAPS_API_KEY set — nothing to fetch.")
        return

    def _read(path):
        try:
            return pd.read_csv(path)
        except (FileNotFoundError, pd.errors.EmptyDataError, pd.errors.ParserError):
            return None

    active = _read(DATA_ACTIVE)
    frames = [f for f in (active, _read(DATA_ARCHIVE)) if f is not None]
    if not frames:
        print(f"No listing data for profile {PROFILE_NAME}.")
        return

    def _located(frame):
        return frame[frame["lat"].notna() & frame["lon"].notna()]

    df        = _located(pd.concat(frames, ignore_index=True).drop_duplicates(subset="url"))
    df_active = _located(active.drop_duplicates(subset="url")) if active is not None else df.iloc[:0]

    try:
        with open(COMMUTE_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, ValueError):
        cache = {}

    known     = set(df["url"])
    stale     = [u for u, v in cache.items() if "segments" not in v]
    reachable = [u for u in stale if u in known]

    missing = [
        r for r in df_active.to_dict("records")
        if r.get("url") and r["url"] not in cache and _is_included(r)
    ]

    budget = _budget_load()
    print(f"Profile {PROFILE_NAME}: {len(cache)} cached, "
          f"{len(reachable)} stale (no per-step geometry), "
          f"{len(missing)} eligible active listing(s) never fetched.")
    print(f"  Calls this month: {budget['calls']}/{_MONTHLY_CALL_CAP}. "
          f"This run would spend up to {len(reachable) + len(missing)}.")
    if len(stale) != len(reachable):
        print(f"  {len(stale) - len(reachable)} stale entr(ies) have no listing "
              f"left to route from and will stay as they are.")

    if args.dry_run:
        print("Dry run — no API calls made.")
        return
    if not reachable and not missing:
        print("Nothing to fetch.")
        return

    # defer_on_limit=False: this is run by hand, so waiting out the rate limit is
    # better than finishing half the job and leaving the map still mostly grey.
    # The monthly cap still applies and is not waited out — it just stops.
    listings = df[df["url"].isin(reachable)].to_dict("records") + missing
    before   = len(cache)
    results  = compute_commutes(listings, defer_on_limit=False)

    # Write the numbers back into the active CSV, not just the cache.
    #
    # The dashboard reads this fact from two places: the map draws a route from
    # the cache, but the dot's opacity, its popup and the commute dropdown all
    # read the CSV column. Filling only the cache therefore published a map
    # whose shortlist listings had a route drawn out of a faded dot whose popup
    # said "Commute not calculated" — the two halves of the same change
    # contradicting each other. The cache is gitignored and the CSV is not, so
    # this is also the only half that reaches the published dashboard at all.
    #
    # Mirrors the write-back in email_alert.py, including the object-dtype dance
    # for the text column: read back from a CSV where every value is empty,
    # pandas types it float64 and writing a line list into it is an
    # incompatible-dtype assignment.
    if results and active is not None:
        csv_df = pd.read_csv(DATA_ACTIVE)
        for col in ("commute_minutes", "commute_walk_minutes", "commute_headway",
                    "transit_score", "transit_lines"):
            if col not in csv_df.columns:
                csv_df[col] = None
            if col == "transit_lines" and csv_df[col].isna().all():
                csv_df[col] = csv_df[col].astype(object)

        written = 0
        for url, info in results.items():
            row = csv_df["url"] == url
            if not row.any():
                continue
            csv_df.loc[row, "commute_minutes"]      = info["minutes"]
            csv_df.loc[row, "commute_walk_minutes"] = info["walk_minutes"]
            csv_df.loc[row, "commute_headway"]      = info["worst_headway"]
            csv_df.loc[row, "transit_score"]        = info["transit_score"]
            csv_df.loc[row, "transit_lines"]        = ", ".join(info["lines"])
            written += 1
        csv_df.to_csv(DATA_ACTIVE, index=False)
        print(f"Wrote commute values into {written} active listing row(s).")

    # Guarded: compute_commutes only writes the cache when it actually fetched
    # something, so a run that fetched nothing (cap already spent, bad API key,
    # no itinerary found) leaves no file at all on a machine that had none —
    # which is exactly the fresh-Pi case this CLI exists for.
    try:
        with open(COMMUTE_CACHE_PATH) as f:
            after_cache = json.load(f)
    except (FileNotFoundError, ValueError):
        after_cache = {}
    still = [u for u, v in after_cache.items() if "segments" not in v]
    print(f"\nCached entries: {before} → {len(after_cache)}")
    if still:
        print(f"{len(still)} still without segments — most likely outside the "
              f"profile's include list, which withholds new calls.")


if __name__ == "__main__":
    _main()
