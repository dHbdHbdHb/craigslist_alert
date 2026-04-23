"""
transit_times.py — compute cycling times from housing listings to the nearest
Caltrain / BART station via OpenRouteService.

Shared by craigslist_scraper.py (populates times eagerly at scrape time so the
dashboard updates within one scrape cycle) and email_alert.py (backfills before
the daily digest for anything the scraper deferred).

Rate limiting:
    ORS free tier is 40 req/min. The module keeps a shared deque of call
    timestamps within the process. When `defer_on_limit=True` the compute
    function stops cold once the per-listing budget would exceed the limit
    and returns what it has — uncomputed listings get picked up on the next
    scrape (cache persists on disk). When `defer_on_limit=False` (the default,
    used by the digest) it sleeps until the window clears, to guarantee all
    listings are computed before the digest goes out.
"""

import json
import os
import time
from collections import deque

import openrouteservice
import pandas as pd

from config import (
    ORS_API_KEY,
    CALTRAIN_4TH_KING_COORDS, CALTRAIN_22ND_ST_COORDS,
    BART_STATIONS,
    DATA_ACTIVE,
)

BIKE_ROUTES_PATH = os.path.join(os.path.dirname(DATA_ACTIVE), "bike_routes.json")
BART_ROUTES_PATH = os.path.join(os.path.dirname(DATA_ACTIVE), "bart_bike_routes.json")

CALTRAIN_STATIONS = [
    ('4th & King', CALTRAIN_4TH_KING_COORDS),
    ('22nd St',    CALTRAIN_22ND_ST_COORDS),
]

_ORS_MAX_PER_MIN = 35  # 40-req/min free tier, with a 5-req safety buffer
_ors_call_times = deque()


def _reserve_ors_slots(slots_needed: int, defer_on_limit: bool) -> bool:
    """Ensure there's room for `slots_needed` upcoming ORS calls.

    Returns True if the caller may proceed to make the calls. When
    `defer_on_limit=True` and capacity isn't available, returns False
    immediately so the caller can bail out. Otherwise sleeps until the oldest
    calls age out of the 60-second window.
    """
    now = time.time()
    while _ors_call_times and _ors_call_times[0] < now - 60:
        _ors_call_times.popleft()
    if len(_ors_call_times) + slots_needed <= _ORS_MAX_PER_MIN:
        return True
    if defer_on_limit:
        return False
    wait = 60 - (now - _ors_call_times[0]) + 0.5
    print(f"    Rate limit approaching, sleeping {wait:.0f}s…")
    time.sleep(wait)
    now = time.time()
    while _ors_call_times and _ors_call_times[0] < now - 60:
        _ors_call_times.popleft()
    return True


def _compute_cycling_times(listings, stations, cache_path, label,
                           defer_on_limit: bool = False) -> dict:
    """For each listing, find the closest station by cycling time via ORS.

    Cache is keyed on listing URL and persists to `cache_path`. Listings that
    are already cached or lack coordinates are skipped without consuming the
    rate-limit budget. When the budget is exhausted, remaining listings are
    left uncomputed so a subsequent run can pick them up.

    Returns {url: {'minutes': int, 'station': str}} for listings that are
    cached or were successfully computed this run.
    """
    ors = openrouteservice.Client(key=ORS_API_KEY, timeout=15)

    try:
        with open(cache_path) as f:
            route_cache = json.load(f)
    except (FileNotFoundError, ValueError):
        route_cache = {}

    result = {}
    deferred_urls = []
    cache_dirty = False
    for i, pt in enumerate(listings):
        url = pt['url']
        cached = route_cache.get(url)
        if cached and 'minutes' in cached and 'station' in cached:
            result[url] = {'minutes': cached['minutes'], 'station': cached['station']}
            continue

        lon, lat = pt.get('lon'), pt.get('lat')
        if not (pd.notna(lon) and pd.notna(lat)):
            print(f"  {label}: skipping {url[-30:]} (no coordinates)")
            continue

        if not _reserve_ors_slots(len(stations), defer_on_limit):
            deferred_urls = [
                p['url'] for p in listings[i:]
                if p.get('url') and p['url'] not in route_cache
            ]
            break

        best_minutes, best_station, best_geom = None, None, None
        for name, coords in stations:
            _ors_call_times.append(time.time())
            try:
                route    = ors.directions(
                    [(lon, lat), (coords[0], coords[1])],
                    profile='cycling-regular', format='geojson',
                )
                minutes  = int(route['features'][0]['properties']['summary']['duration'] / 60)
                geom_raw = route['features'][0]['geometry']['coordinates']
                if best_minutes is None or minutes < best_minutes:
                    best_minutes = minutes
                    best_station = name
                    best_geom    = [[c[1], c[0]] for c in geom_raw]
            except Exception as e:
                print(f"  {label} ORS error for {url[-30:]} → {name}: {e}")

        if best_minutes is None:
            print(f"  {label}: all routes failed for {url[-30:]}, skipping")
            continue

        result[url]      = {'minutes': best_minutes, 'station': best_station}
        route_cache[url] = {
            'minutes':  best_minutes,
            'station':  best_station,
            'geometry': best_geom,
        }
        cache_dirty = True
        print(f"  {label}: {url[-30:]} → {best_minutes} min to {best_station}")

    if deferred_urls:
        print(f"  {label}: rate limit hit — deferred {len(deferred_urls)} "
              f"listing(s) to next run")

    if cache_dirty:
        try:
            with open(cache_path, 'w') as f:
                json.dump(route_cache, f)
        except OSError as e:
            print(f"  {label}: could not write cache ({e})")

    return result


def compute_bike_times(listings, defer_on_limit: bool = False) -> dict:
    return _compute_cycling_times(
        listings, CALTRAIN_STATIONS, BIKE_ROUTES_PATH, 'Caltrain',
        defer_on_limit=defer_on_limit,
    )


def compute_bart_bike_times(listings, defer_on_limit: bool = False) -> dict:
    return _compute_cycling_times(
        listings, BART_STATIONS, BART_ROUTES_PATH, 'BART',
        defer_on_limit=defer_on_limit,
    )
