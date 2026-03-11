"""
test_images.py — check what image data is available in Craigslist search results.

We want to know:
1. Do the HTML listing cards include thumbnail image URLs?
2. Does the JSON-LD include image data?
3. If not, what does a single listing page look like?

Run locally: python test_images.py
"""

import requests
from bs4 import BeautifulSoup
import json
import re

URL = "https://sfbay.craigslist.org/search/apa?max_price=5600&min_bedrooms=2"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0.0.0 Safari/537.36"
    )
}

resp = requests.get(URL, headers=headers)
soup = BeautifulSoup(resp.text, "html.parser")

# ── Check 1: JSON-LD ──────────────────────────────────────────────────────────
print("=== JSON-LD image fields ===")
script = soup.find("script", {"id": "ld_searchpage_results"})
if script:
    data = json.loads(script.string)
    items = data.get("itemListElement", [])
    item = items[0].get("item", {}) if items else {}
    image_fields = {k: v for k, v in item.items() if "image" in k.lower() or "photo" in k.lower()}
    print(f"Image-related keys in first item: {image_fields or 'none'}")
    print(f"All keys in first item: {list(item.keys())}")

# ── Check 2: HTML listing cards ───────────────────────────────────────────────
print("\n=== Images in HTML listing cards ===")
listing_links = [
    a for a in soup.find_all('a', href=True)
    if re.search(r'/apa/d/.+?/\d+\.html', a['href'])
]
print(f"Listing cards found: {len(listing_links)}")

if listing_links:
    first = listing_links[0]
    imgs = first.find_all('img')
    print(f"<img> tags in first card: {len(imgs)}")
    for img in imgs:
        print(f"  src: {img.get('src')}")
        print(f"  attrs: {dict(img.attrs)}")

# ── Check 3: Fetch a single listing page ─────────────────────────────────────
print("\n=== Single listing page ===")
if listing_links:
    listing_url = listing_links[0]['href']
    print(f"Fetching: {listing_url}")
    listing_resp = requests.get(listing_url, headers=headers)
    listing_soup = BeautifulSoup(listing_resp.text, "html.parser")

    # Craigslist listing pages often have images in a JSON blob
    image_json = None
    for s in listing_soup.find_all("script"):
        if s.string and "imgList" in (s.string or ""):
            image_json = s.string
            break

    if image_json:
        match = re.search(r'"imgList":\s*(\[.*?\])', image_json, re.DOTALL)
        if match:
            try:
                img_list = json.loads(match.group(1))
                print(f"imgList entries: {len(img_list)}")
                print(f"First entry: {img_list[0]}")
            except Exception as e:
                print(f"Parse error: {e}")
    else:
        # Fallback: look for og:image meta tag
        og_image = listing_soup.find("meta", property="og:image")
        if og_image:
            print(f"og:image: {og_image.get('content')}")
        else:
            imgs = listing_soup.find_all("img", src=re.compile(r'images\.craigslist'))
            print(f"Craigslist <img> tags on listing page: {len(imgs)}")
            if imgs:
                print(f"First: {imgs[0].get('src')}")
