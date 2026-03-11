"""
test_apartments.py — exploratory script to see what Apartments.com returns.

Run locally: python test_apartments.py
"""

import requests
from bs4 import BeautifulSoup
import json

URL = "https://www.apartments.com/san-francisco-ca/2-bedrooms/?max-price=5600"

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

resp = requests.get(URL, headers=headers, timeout=15)
print(f"Status: {resp.status_code}")
print(f"Content-Type: {resp.headers.get('Content-Type')}")
print(f"Response length: {len(resp.text)} chars")
print()

if resp.status_code != 200:
    print("Non-200 response. Body snippet:")
    print(resp.text[:500])
else:
    soup = BeautifulSoup(resp.text, "html.parser")

    # Check for JSON-LD data (like Craigslist uses)
    json_ld = soup.find_all("script", type="application/ld+json")
    print(f"JSON-LD blocks found: {len(json_ld)}")
    for i, block in enumerate(json_ld[:3]):
        try:
            data = json.loads(block.string)
            print(f"\n--- JSON-LD block {i} ---")
            print(json.dumps(data, indent=2)[:500])
        except Exception:
            pass

    # Check for listing cards in HTML
    # Apartments.com uses <article> tags or data-listingid attributes for listings
    articles = soup.find_all("article")
    listing_ids = soup.find_all(attrs={"data-listingid": True})
    print(f"\nArticle tags: {len(articles)}")
    print(f"Elements with data-listingid: {len(listing_ids)}")

    # Check if content looks JS-rendered (empty shell)
    if len(articles) == 0 and len(listing_ids) == 0:
        print("\nLikely JS-rendered — no listing elements in raw HTML")
    else:
        print("\nListing elements found in HTML — looks parseable!")
        if listing_ids:
            print("\n--- First listing element ---")
            print(listing_ids[0].prettify()[:800])
