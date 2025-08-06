# Housing Alert System


## Neighborhood filtering

Edit `neighborhood_shapes.py` to define polygons. Listings are tagged with one or more neighborhood names if their lat/lon intersects.

### Priority logic

In `craigslist_alert_robust.py`, adjust:

```python
priority_neighborhoods = {"Mission", "Duboce"}
priority_max_price = 3700
priority_min_bathrooms = 2
```

### Digest criteria

All non-priority listings are grouped and sent daily as a digest, with listings grouped by neighborhood and mapped.
Both scraper and alert scipts run via cron jobs so have to make sure that is setup too.

---

## File Structure

```
craigslist_alert/
├── craigslist_scraper.py         # Main scraper logic
├── craigslist_alert_robust.py    # Alert logic with priority & digest
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

## For Future

* Add Zillow and Facebook Marketplace support
* Move to a web dashboard maybe
* Better digest formatting templates

---
