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
"""

import json
import time
from datetime import datetime, timedelta, timezone
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


def _is_included(listing) -> bool:
    """True if the listing is in at least one neighborhood the profile lists.

    An empty include list means "everywhere", matching how the digest and the
    dashboard already read it.
    """
    if not _INCLUDED_HOODS:
        return True
    hoods = str(listing.get("neighborhoods") or "")
    return any(h.strip() in _INCLUDED_HOODS for h in hoods.split(","))


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
    consecutive_failures = 0

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

        if not _reserve_slot(defer_on_limit):
            deferred = sum(
                1 for p in listings[i:] if p.get("url") and p["url"] not in cache
            )
            break

        _call_times.append(time.time())
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

    if cache_dirty:
        try:
            with open(COMMUTE_CACHE_PATH, "w") as f:
                json.dump(cache, f)
        except OSError as e:
            print(f"  Commute: could not write cache ({e})")

    return result


def _describe(info: dict) -> str:
    """One-line human summary, e.g. '32 min to work · 7 min walk to transit'.

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
    return " · ".join(bits)


# ── Backfill CLI ──────────────────────────────────────────────────────────────
#
# compute_commutes() already refetches any entry missing "segments", but nothing
# ever offers it the listings that need it: the scraper passes only *new*
# records and the digest only the listings going in that email. An entry cached
# before per-step geometry was requested therefore stays stale forever, and the
# map draws it as one undifferentiated "Other transit" line -- which is how 32
# of 49 cached trips ended up grey despite being ordinary bus/Muni/BART rides.
#
# This walks the full active + archive set instead, so those entries get seen
# exactly once. Complete entries cost nothing: the cache check skips them before
# any request is made, so the bill is one call per genuinely stale listing.
#
#     python transit_commute.py --dry-run     # count them, spend nothing
#     python transit_commute.py               # refetch
def _main() -> None:
    import argparse

    from config import DATA_ACTIVE, DATA_ARCHIVE, add_profile_arg, PROFILE_NAME

    parser = argparse.ArgumentParser(
        description="Refetch cached commutes that predate per-step geometry."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be refetched; make no API calls.")
    add_profile_arg(parser)
    args = parser.parse_args()

    frames = []
    for path in (DATA_ACTIVE, DATA_ARCHIVE):
        try:
            frames.append(pd.read_csv(path))
        except (FileNotFoundError, pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
    if not frames:
        print(f"No listing data for profile {PROFILE_NAME}.")
        return

    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="url")
    df = df[df["lat"].notna() & df["lon"].notna()]

    try:
        with open(COMMUTE_CACHE_PATH) as f:
            cache = json.load(f)
    except (FileNotFoundError, ValueError):
        cache = {}

    stale = [u for u, v in cache.items() if "segments" not in v]
    known = set(df["url"])
    reachable = [u for u in stale if u in known]

    print(f"Profile {PROFILE_NAME}: {len(cache)} cached, {len(stale)} missing "
          f"per-step geometry, {len(reachable)} of those still in the listing data.")
    if len(stale) != len(reachable):
        print(f"  {len(stale) - len(reachable)} stale entr(ies) have no listing "
              f"left to route from and will stay as they are.")

    if args.dry_run:
        print("Dry run — no API calls made.")
        return
    if not reachable:
        print("Nothing to refetch.")
        return

    # defer_on_limit=False: this is run by hand, so waiting out the rate limit is
    # better than finishing half the job and leaving the map still mostly grey.
    listings = df[df["url"].isin(reachable)].to_dict("records")
    before   = sum(1 for v in cache.values() if "segments" in v)
    compute_commutes(listings, defer_on_limit=False)

    with open(COMMUTE_CACHE_PATH) as f:
        after_cache = json.load(f)
    after = sum(1 for v in after_cache.values() if "segments" in v)
    still = [u for u, v in after_cache.items() if "segments" not in v]
    print(f"\nEntries with per-step geometry: {before} → {after}")
    if still:
        print(f"{len(still)} still without segments — most likely outside the "
              f"profile's include list, which withholds new calls.")


if __name__ == "__main__":
    _main()
