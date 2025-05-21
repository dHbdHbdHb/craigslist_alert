# Housing Alert System

I just want to find housing in sf 😭.  I built an automated, Raspberry Pi-powered Python application that:

* Scrapes Craigslist for new apartment listings using configurable criteria
* Tags listings by neighborhood using geospatial filtering
* Sends **individual alert emails** for stuff that looks good
* Sends a **daily digest email** with all new listings, including a static map showing bike routes to 4th & King Caltrain for peninsula commuters (😭x2)
* Automatically archives old listings and persists all data in CSVs

## Features

* **Neighborhood detection** via shapely polygons
* **Listing filters** for price, bedrooms, bathrooms, and city
* **Custom prioritization** for urgent alerts
* **Email alerts** via Gmail with HTML formatting
* **Static map generation** with estimated bike times
* **Runs on boot & every 30 minutes** using cron
* **CSV persistence and archiving** for long-term analysis

---

## Customization

### Neighborhood filtering

Edit `neighborhood_shapes.py` to define polygons. Listings are tagged with one or more neighborhood names if their lat/lon intersects.

### Priority logic

In `craigslist_alert.py`, adjust:

```python
priority_neighborhoods = {"Mission", "Duboce"}
priority_max_price = 3700
priority_min_bathrooms = 2
```

### Digest criteria

All non-priority listings are grouped and sent daily as a digest, with listings grouped by neighborhood and mapped.

---

## File Structure

```
craigslist_alert/
├── craigslist_scraper.py         # Main scraper logic
├── craigslist_alert.py           # Alert logic with priority & digest
├── shell_scripts/
│   ├── run_scraper.sh            # Wrapper for cron
│   ├── run_alert.sh              # Wrapper for cron
├── neighborhood_shapes.py        # Defines geospatial filters
├── craigslist_data/
│   ├── listings_active.csv       # Active listings
│   └── listings_archive.csv      # Archived listings
├── logs/
│   ├── scraper.log
│   └── alert.log
└── environment.yml               # Conda env definition
```

---

## Future Ideas

* Add Zillow and Facebook Marketplace support
* Move to a web dashboard
* Better digest formatting using templates

---
