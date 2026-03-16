# Housing Alert System

Scrapes Craigslist SF apartment listings, filters by neighborhood, and sends email alerts. Runs on a Raspberry Pi via cron.

- **Priority alerts** — immediate individual email when a listing matches priority neighborhoods, price cap, and bathroom minimum
- **Daily digest** — one email per day grouping all new listings by neighborhood, with a biking-time map from Caltrain

---

## Configuration

All shared settings live in `config.py`:

```python
# Alert criteria
priority_neighborhoods = {"Chill Mission", "Duboce", "NOPA/Inner Richmond", ...}
priority_max_price     = 3000
priority_min_bathrooms = 2
digest_max_price       = 5500

# Email
GMAIL_ADDRESS           = "..."
DIGEST_RECIPIENT_EMAILS = ["..."]
ALERT_RECIPIENT_EMAILS  = ["..."]
```

Neighborhood polygons are defined in `neighborhoods/neighborhood_shapes.py`. Listings are tagged with any neighborhood whose polygon contains the listing's lat/lon.

---

## File Structure

```
craigslist_alert/
├── config.py                         # All shared config (email, paths, alert criteria)
├── craigslist_scraper.py             # Craigslist scraper — fetches & stores listings
├── email_alert.py                    # Alert logic — priority emails + daily digest
├── neighborhoods/
│   └── neighborhood_shapes.py        # Geospatial polygon definitions
├── shell_scripts/
│   ├── run_scraper.sh                # Cron wrapper for scraper
│   ├── run_alert.sh                  # Cron wrapper for alert
│   ├── upload_csv.sh                 # Git commit & push updated CSVs
│   └── update_env_yaml.sh            # Utility: refresh environment.yml
├── craigslist_data/
│   ├── listings_active.csv           # Current listings (max 1000 rows)
│   └── listings_archive.csv          # Overflow archive
├── logs/
│   ├── scraper.log
│   ├── alert.log
│   └── git.log
├── environment.yml                   # Conda environment definition
└── last_digest_date.txt              # Tracks last digest date (prevents duplicates)
```

---

## Raspberry Pi Setup

1. Flash Pi OS, set hostname `craig-pi`, username `pi`, enable SSH
2. Install Miniforge: `bash Miniforge3-Linux-aarch64.sh`
3. Clone repo: `git clone git@github.com:dHbdHbdHb/craigslist_alert.git ~/craigslist_alert`
4. Create conda env: `conda env create -f environment.yml`
5. Install missing deps: `pip install openrouteservice selenium`
6. Install ChromeDriver: `sudo apt install chromium-driver`
7. Make scripts executable: `chmod +x shell_scripts/*.sh`
8. Add cron jobs (`crontab -e`):
   ```
   0 4,10,16,22 * * * /home/pi/craigslist_alert/shell_scripts/run_scraper.sh >> /home/pi/craigslist_alert/logs/scraper.log 2>&1
   5 4,10,16,22 * * * /home/pi/craigslist_alert/shell_scripts/run_alert.sh >> /home/pi/craigslist_alert/logs/alert.log 2>&1
   10 4,10,16,22 * * * /home/pi/craigslist_alert/shell_scripts/upload_csv.sh >> /home/pi/craigslist_alert/logs/git.log 2>&1
   ```

---

## Adding a New Scraper

Each scraper should:
- Import shared paths from `config.py`
- Write rows with the same schema as `craigslist_scraper.py`
- Set `source` to the site name (e.g. `'zillow'`, `'facebook'`)
- Append to `DATA_ACTIVE` (not overwrite)

The alert script is source-agnostic and requires no changes.

---