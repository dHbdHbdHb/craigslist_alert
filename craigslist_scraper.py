import requests
from bs4 import BeautifulSoup
import json
import re
from shapely.geometry import Point
from neighborhoods.neighborhood_shapes import neighborhood_shapes
import pandas as pd
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# ----- Configurable parameters -----
max_price = "5600"
min_bedrooms = "2"
BASE_DIR = os.path.expanduser("~/craigslist_alert")
DATA_ACTIVE  = os.path.join(BASE_DIR, "craigslist_data", "listings_active.csv")
DATA_ARCHIVE = os.path.join(BASE_DIR, "craigslist_data", "listings_archive.csv")
MAX_ACTIVE_ROWS = 1000

SEARCH_URL = (
    f"https://sfbay.craigslist.org/search/apa?max_price={max_price}&min_bedrooms={min_bedrooms}"
)

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0.0.0 Safari/537.36"
    )
}

def assign_neighborhoods(lon, lat, hood_shapes):
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
    # Remove dollar sign and commas
    clean = price_str.replace('$', '').replace(',', '')
    return parse_num(clean)

def main():
    # Ensure data directory exists
    os.makedirs(os.path.dirname(DATA_ACTIVE), exist_ok=True)

    # Load existing active listings
    if os.path.exists(DATA_ACTIVE):
        try:
            df_old = pd.read_csv(DATA_ACTIVE)
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            df_old = pd.DataFrame()
    else:
        df_old = pd.DataFrame()

    # Fetch Craigslist search results
    resp = requests.get(SEARCH_URL, headers=headers)
    soup = BeautifulSoup(resp.text, "html.parser")
    script = soup.find("script", {"id": "ld_searchpage_results"})
    if not script:
        print("No search results found in JSON-LD!")
        return
    data = json.loads(script.string)
    items = data.get("itemListElement", [])

    # Extract post links, titles, and prices from HTML
    post_links = []
    for a in soup.find_all('a', href=True):
        if re.search(r'/apa/d/.+?/\d+\.html', a['href']):
            title_div = a.find('div', class_='title')
            title = title_div.text.strip() if title_div else 'No title'
            price_div = a.find('div', class_='price')
            price_html = price_div.text.strip() if price_div else None
            post_links.append({'url': a['href'], 'title': title, 'raw_price': price_html})

    # Build new listings DataFrame with city filter
    listings = []
    for idx, item in enumerate(items):
        info = item.get("item", {})
        lat = info.get("latitude")
        lon = info.get("longitude")
        if lat is None or lon is None:
            continue
        city = info.get('address', {}).get('addressLocality', '')
        # Only include SF listings
        if city != 'San Francisco':
            continue

        # Capture overlapping neighborhoods
        hoods = assign_neighborhoods(lon, lat, neighborhood_shapes)
        hood_str = ",".join([h for h in hoods if h and h != 'None'])

        # Get HTML-derived data
        link_info = post_links[idx] if idx < len(post_links) else {}
        price = clean_price(link_info.get('raw_price'))
        beds = parse_num(info.get('numberOfBedrooms'))
        baths = parse_num(info.get('numberOfBathroomsTotal'))
        post_time = info.get('datePosted') or datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()

        listings.append({
            'title': info.get('name', 'No title'),
            'neighborhoods': hood_str,
            'price': price,
            'num_bedrooms': beds,
            'num_bathrooms': baths,
            'url': link_info.get('url'),
            'lat': lat,
            'lon': lon,
            'city': city,
            'time_posted': post_time,
            'alerted': False,
            'priority_alerted': False
        })

    df_new = pd.DataFrame(listings)

    # Deduplicate by URL
    if not df_old.empty:
        new_mask = ~df_new['url'].isin(df_old['url'])
        df_result = pd.concat([df_old, df_new[new_mask]], ignore_index=True)
    else:
        df_result = df_new

    # Ensure all necessary columns exist
    for col in ("alerted", "priority_alerted"):
        if col not in df_result.columns:
            df_result[col] = False
    df_result[["alerted", "priority_alerted"]] = (
        df_result[["alerted", "priority_alerted"]]
        .fillna(False)
        .astype(bool)
    )

    # Split into active and archive
    df_result = df_result.sort_values('time_posted', ascending=False)
    df_active = df_result.head(MAX_ACTIVE_ROWS)
    df_archive_candidates = df_result.iloc[MAX_ACTIVE_ROWS:]

    # Save active listings
    print(
        f"Writing {len(df_active)} rows to {DATA_ACTIVE} "
        f"at {datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    df_active.to_csv(DATA_ACTIVE, index=False)

    # Append new to archive
    if os.path.exists(DATA_ARCHIVE):
        df_archive = pd.read_csv(DATA_ARCHIVE)
        to_add = df_archive_candidates[~df_archive_candidates['url'].isin(df_archive['url'])]
        df_archive = pd.concat([df_archive, to_add], ignore_index=True)
    else:
        df_archive = df_archive_candidates
    df_archive.to_csv(DATA_ARCHIVE, index=False)

    # Summary
    print(f"Scraped {len(df_new)} listings; {len(df_active)} active; {len(df_archive)} archived.")
    print(df_active.head())

if __name__ == '__main__':
    main()
