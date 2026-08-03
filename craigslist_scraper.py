import requests
from bs4 import BeautifulSoup
import json
import re
from shapely.geometry import Point
from neighborhoods.neighborhood_shapes import neighborhood_shapes
import pandas as pd
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import (
    DATA_ACTIVE, DATA_ARCHIVE, MAX_ACTIVE_ROWS,
    SEARCH_URL, SEARCH_CITY, PROFILE_NAME,
)
from transit_times import compute_bike_times, compute_bart_bike_times

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0.0.0 Safari/537.36"
    )
}

def is_listing_active(url: str) -> bool:
    """Return False if the listing has been flagged or deleted, True otherwise.
    On network errors, returns True (keep the listing rather than incorrectly purge it)."""
    try:
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
        flagged = "This posting has been flagged for removal."
        deleted = "This posting has been deleted by its author."
        return not (flagged in response.text or deleted in response.text)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 410:
            return False
        return True  # unknown HTTP error — keep the listing
    except requests.RequestException:
        return True  # network error — keep the listing


def purge_inactive_listings(df: pd.DataFrame, fresh_urls: set):
    """Split df into (still-live, newly-removed) listings.

    Only checks listings whose URL was NOT returned in the latest scrape —
    fresh results are obviously still active. Adds a small delay between
    requests to avoid hammering Craigslist.

    Removed listings used to be dropped on the floor here, which quietly
    destroyed the most interesting part of the price history: the listings that
    disappeared fastest are the ones that actually rented. They're now returned
    separately so the caller can append them to the archive with a removal
    timestamp.
    """
    empty = df.iloc[0:0]
    to_check = df[~df['url'].isin(fresh_urls)].copy()
    if to_check.empty:
        return df, empty

    print(f"Checking {len(to_check)} existing listings for removal...")
    inactive_urls = set()
    for url in to_check['url'].dropna():
        if not is_listing_active(url):
            print(f"  Archiving removed listing: {url}")
            inactive_urls.add(url)
        time.sleep(0.3)

    if not inactive_urls:
        return df, empty

    print(f"  {len(inactive_urls)} listing(s) removed from Craigslist — archiving.")
    removed = df[df['url'].isin(inactive_urls)].copy()
    removed['removed_at'] = datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()

    still_live = df[~df['url'].isin(inactive_urls)].reset_index(drop=True)
    return still_live, removed


def append_to_archive(new_rows: pd.DataFrame) -> None:
    """Append rows to the archive CSV, skipping URLs already recorded there.

    The archive is append-only: it's the historical record, and nothing else
    writes to it.
    """
    if new_rows.empty:
        return

    os.makedirs(os.path.dirname(DATA_ARCHIVE), exist_ok=True)

    if os.path.exists(DATA_ARCHIVE):
        try:
            existing = pd.read_csv(DATA_ARCHIVE)
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            existing = pd.DataFrame()
    else:
        existing = pd.DataFrame()

    if not existing.empty and 'url' in existing.columns:
        new_rows = new_rows[~new_rows['url'].isin(existing['url'])]
        if new_rows.empty:
            return
        combined = pd.concat([existing, new_rows], ignore_index=True)
    else:
        combined = new_rows

    combined.to_csv(DATA_ARCHIVE, index=False)
    print(f"  Archived {len(new_rows)} listing(s) → {len(combined)} total in archive.")


def assign_neighborhoods(lon, lat, hood_shapes):
    """Return list of neighborhood names whose polygons contain this point."""
    pt = Point(lon, lat)
    hoods = [hood for hood, poly in hood_shapes.items() if poly.contains(pt)]
    return hoods if hoods else [None]

def parse_num(s):
    try:
        return int(s)
    except:
        try:
            return float(s)
        except:
            return None

def clean_price(price_str):
    if not price_str:
        return None
    return parse_num(price_str.replace('$', '').replace(',', ''))

def main():
    # Ensure data directory exists
    os.makedirs(os.path.dirname(DATA_ACTIVE), exist_ok=True)

    # Load existing active listings, gracefully handling empty or malformed files
    if os.path.exists(DATA_ACTIVE):
        try:
            df_old = pd.read_csv(DATA_ACTIVE)
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            df_old = pd.DataFrame()
    else:
        df_old = pd.DataFrame()

    # Fetch Craigslist search results page
    resp = requests.get(SEARCH_URL, headers=headers)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Listings are embedded as JSON-LD in a <script> tag
    script = soup.find("script", {"id": "ld_searchpage_results"})
    if not script:
        print("No search results found in JSON-LD!")
        return
    data = json.loads(script.string)
    items = data.get("itemListElement", [])

    # Extract URLs, titles and prices from the HTML listing cards.
    #
    # The JSON-LD block carries coordinates, beds and baths but no URL or price,
    # so those have to come from the cards, matched to the JSON-LD entries by
    # position. Craigslist moved to /view/d/<slug>/<base62-id> URLs and dropped
    # the old sfbay.craigslist.org/apa/d/<slug>/<id>.html scheme, so selecting
    # on the list item is more durable than matching the href pattern.
    post_links = []
    for card in soup.select('li.cl-static-search-result'):
        anchor    = card.find('a', href=True)
        title_div = card.find('div', class_='title')
        price_div = card.find('div', class_='price')
        post_links.append({
            'url':       anchor['href'] if anchor else None,
            'title':     title_div.get_text(strip=True) if title_div else 'No title',
            'raw_price': price_div.get_text(strip=True) if price_div else None,
        })

    # Positional matching only holds if both lists are the same length. If
    # Craigslist changes the markup again this is the first thing that breaks,
    # so say so loudly rather than silently mislabelling every listing.
    if len(post_links) != len(items):
        print(f"WARNING: {len(items)} JSON-LD entries but {len(post_links)} listing "
              f"cards. Craigslist's markup may have changed — prices and URLs "
              f"could be mismatched. Check the 'li.cl-static-search-result' "
              f"selector in craigslist_scraper.py.")

    # Build new listings, filtering to the profile's city and assigning
    # neighborhoods. An empty SEARCH_CITY keeps every city in the region.
    listings = []
    skipped_no_url = 0
    for idx, item in enumerate(items):
        info = item.get("item", {})
        lat = info.get("latitude")
        lon = info.get("longitude")
        if lat is None or lon is None:
            continue
        if SEARCH_CITY and info.get('address', {}).get('addressLocality', '') != SEARCH_CITY:
            continue

        hoods = assign_neighborhoods(lon, lat, neighborhood_shapes)
        hood_str = ",".join([h for h in hoods if h and h != 'None'])

        link_info = post_links[idx] if idx < len(post_links) else {}

        # The JSON-LD block and the HTML listing cards are matched by position,
        # and Craigslist sometimes returns more of the former than the latter.
        # A row with no URL can't be linked to, deduplicated, or routed against,
        # so drop it instead of storing an unusable listing.
        if not link_info.get('url'):
            skipped_no_url += 1
            continue

        post_time = info.get('datePosted') or datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()

        listings.append({
            'source': 'craigslist',  # identifies origin for multi-source support
            'title': info.get('name', 'No title'),
            'neighborhoods': hood_str,
            'price': clean_price(link_info.get('raw_price')),
            'num_bedrooms': parse_num(info.get('numberOfBedrooms')),
            'num_bathrooms': parse_num(info.get('numberOfBathroomsTotal')),
            'url': link_info.get('url'),
            'lat': lat,
            'lon': lon,
            'city': info.get('address', {}).get('addressLocality', ''),
            'time_posted': post_time,
            'alerted': False,
            'bike_time_minutes': None,  # filled in by email_alert.py when digest is sent
        })

    if skipped_no_url:
        print(f"  Skipped {skipped_no_url} listing(s) with no URL.")

    df_new = pd.DataFrame(listings)
    fresh_urls = set(df_new['url'].dropna()) if not df_new.empty else set()

    # Move flagged/deleted listings out of the active set before merging.
    # They go to the archive rather than being discarded.
    df_removed = pd.DataFrame()
    if not df_old.empty:
        df_old, df_removed = purge_inactive_listings(df_old, fresh_urls)

    # Merge with existing listings, deduplicating by URL
    if not df_old.empty and not df_new.empty:
        new_mask = ~df_new['url'].isin(df_old['url'])
        truly_new = df_new[new_mask]
        df_result = pd.concat([df_old, truly_new], ignore_index=True)
    elif not df_new.empty:
        truly_new = df_new
        df_result = df_new
    else:
        truly_new = pd.DataFrame()
        df_result = df_old

    # Ensure alerted column exists and is boolean
    if 'alerted' not in df_result.columns:
        df_result['alerted'] = False
    df_result['alerted'] = df_result['alerted'].fillna(False).astype(bool)

    # Ensure transit-time columns exist
    for _col in ('bike_time_minutes', 'bike_station',
                 'bart_bike_time_minutes', 'bart_station'):
        if _col not in df_result.columns:
            df_result[_col] = None

    # Compute Caltrain/BART cycling times for newly-scraped listings in a known
    # neighborhood, so the dashboard reflects them on the next upload (instead
    # of waiting for the daily digest). defer_on_limit=True means we bail out
    # of ORS calls when the 40-req/min budget is exhausted — leftover listings
    # get retried on the next scrape run (cache persists on disk).
    if not truly_new.empty:
        to_compute = truly_new[truly_new['neighborhoods'].fillna('').astype(str).str.strip() != '']
        if not to_compute.empty:
            new_records = to_compute.to_dict('records')
            bike_times      = compute_bike_times(new_records, defer_on_limit=True)
            bart_bike_times = compute_bart_bike_times(new_records, defer_on_limit=True)
            for url, info in bike_times.items():
                df_result.loc[df_result['url'] == url, 'bike_time_minutes'] = info['minutes']
                df_result.loc[df_result['url'] == url, 'bike_station']      = info['station']
            for url, info in bart_bike_times.items():
                df_result.loc[df_result['url'] == url, 'bart_bike_time_minutes'] = info['minutes']
                df_result.loc[df_result['url'] == url, 'bart_station']           = info['station']

    # Nothing scraped and nothing stored yet: leave without writing an empty CSV.
    # Happens on a brand-new profile whose filters match nothing, and whenever
    # Craigslist returns an unexpected page.
    if df_result.empty or 'time_posted' not in df_result.columns:
        print(f"[{PROFILE_NAME}] No listings to save "
              f"({len(df_new)} scraped, {len(df_old)} existing).")
        return

    # Keep only the most recent MAX_ACTIVE_ROWS; overflow goes to archive
    df_result = df_result.sort_values('time_posted', ascending=False)
    df_active = df_result.head(MAX_ACTIVE_ROWS)
    df_archive_candidates = df_result.iloc[MAX_ACTIVE_ROWS:]

    df_active.to_csv(DATA_ACTIVE, index=False)
    print(f"[{PROFILE_NAME}] Scraped {len(df_new)} listings; {len(df_active)} active.")

    # Everything leaving the active set goes to the archive: listings taken down
    # on Craigslist, plus anything pushed out by the MAX_ACTIVE_ROWS cap.
    to_archive = [d for d in (df_removed, df_archive_candidates) if not d.empty]
    if to_archive:
        append_to_archive(pd.concat(to_archive, ignore_index=True))


if __name__ == '__main__':
    main()
