# Housing Alert System

Scrapes Craigslist SF apartment listings, filters by neighborhood, and sends
email alerts. Runs on a Raspberry Pi via cron.

- **Daily digest** — one email per day grouping new listings by neighborhood,
  each with its door-to-door transit commute
- **Dashboard** — Plotly charts and a Folium map, rebuilt every 10 minutes

Immediate per-listing "priority alerts" were removed — in practice they mostly
caught scams, which `[alerts.digest] min_posting_age_minutes` and
`scam_keywords` now handle instead.

**Setting this up for yourself? Read [SETUP.md](SETUP.md).** This file is the
architecture reference.

---

## Profiles

The system is multi-tenant: several people share one Pi, each with their own
settings, data, and dashboard. A profile is one person's search.

```
profiles/daniel.toml      →  data/daniel/     →  dashboards/daniel.html
profiles/alex.toml        →  data/alex/       →  dashboards/alex.html
```

Everything personal is in `profiles/<name>.toml` — budget, neighborhoods, alert
thresholds, recipients. Nothing secret goes in there; it's committed to git.

Every script takes `--profile NAME`, falls back to `$HOUSING_PROFILE`, and
finally to the single enabled profile if there's exactly one:

```bash
python craigslist_scraper.py --profile alex
HOUSING_PROFILE=alex python analyze_listings.py
```

Validate a profile without running anything:

```bash
python search_profile.py           # check all
python search_profile.py alex      # check one
```

### Shared vs per-profile

| Shared across everyone | Per profile |
|---|---|
| Gmail sender (`secrets.env`) | Recipients |
| OpenRouteService key | Budget, bedrooms, region/subarea, city |
| Google Maps key | Which neighborhoods |
| Neighborhood shapes | Commute destination and its scoring weights |
| The Pi and its cron jobs | Digest thresholds, transit anchors, dashboard |

---

## Secrets

Credentials live in `secrets.env`, which is **gitignored and must never be
committed**. Copy the template and fill it in:

```bash
cp secrets.env.example secrets.env
```

Real environment variables override the file, so cron or CI can inject values
without touching disk.

---

## File Structure

```
craigslist_alert/
├── profiles/
│   ├── example.toml              # template — copy, don't edit
│   └── <name>.toml               # one person's search settings
├── secrets.env                   # shared credentials (GITIGNORED)
├── secrets.env.example           # template for the above
├── search_profile.py             # profile loading + validation, and a CLI to check them
├── config.py                     # resolves the active profile, re-exports it as constants
├── craigslist_scraper.py         # scraper — fetches, geocodes, stores listings
├── email_alert.py                # the daily digest
├── analyze_listings.py           # builds the HTML dashboard
├── transit_times.py              # cycling times to a station (ORS), on-disk route cache
├── transit_commute.py            # door-to-door transit to work (Google Routes), cached
├── migrate_to_profiles.py        # one-time migration from the old single-user layout
├── neighborhoods/
│   ├── neighborhood_shapes.py    # shared polygon definitions
│   ├── edit_neighborhoods.py     # browser map editor; writes the file above
│   └── draft_shapes.geojson      # proposed shapes awaiting review in the editor
├── shell_scripts/
│   ├── _common.sh                # conda activation + per-profile looping
│   ├── run_scraper.sh            # cron: scrape all enabled profiles
│   ├── run_alert.sh              # cron: alerts + digest for all enabled profiles
│   ├── upload_csv.sh             # cron: rebuild dashboards, commit & push
│   ├── dns_probe.sh              # root cron: DNS watchdog with escalating recovery
│   └── update_env_yaml.sh        # utility: refresh environment.yml
├── data/
│   ├── <name>/
│   │   ├── listings_active.csv   # current listings (max 1000 rows)
│   │   ├── listings_archive.csv  # append-only history: removed + overflow
│   │   ├── bike_routes.json      # ORS route cache (gitignored)
│   │   ├── transit_commutes.json # Google Routes commute cache (gitignored)
│   │   └── last_digest_date.txt  # duplicate-send guard (gitignored)
│   └── historical/
│       ├── 2026-sf.csv           # frozen Mar–Apr 2026 archive (read-only)
│       └── README.md             # what it is, and its caveats
├── dashboards/<name>.html        # generated dashboards
├── logs/                         # cron output (gitignored)
└── environment.yml               # conda environment definition
```

---

## Data model

`listings_active.csv` is the working set, capped at `MAX_ACTIVE_ROWS` (1000).
`listings_archive.csv` is **append-only history** and receives two things:

1. listings taken down on Craigslist (flagged or deleted), stamped with
   `removed_at`
2. listings pushed out of the active set by the row cap

The first case matters: listings that disappear fastest are the ones that
actually rented, which makes them the most informative rows in the dataset.
They used to be discarded outright. Anything analysing price history should read
both CSVs — `analyze_listings.load_data()` already does.

`data/historical/` is a separate frozen dataset and is never written to after
its initial freeze. The dashboard draws it as its own series so it can show
long-run price movement without those months-old listings distorting current
medians, counts, or heatmaps. Rows already present in live data are dropped from
it on load, so nothing is ever counted twice.

---

## Raspberry Pi Setup

1. Flash Pi OS, set hostname `craig-pi`, username `pi`, enable SSH
2. Install Miniforge: `bash Miniforge3-Linux-aarch64.sh`
3. Clone: `git clone git@github.com:dHbdHbdHb/craigslist_alert.git ~/craigslist_alert`
4. Create conda env: `conda env create -f environment.yml`
5. Install missing deps: `pip install openrouteservice selenium`
6. Install ChromeDriver: `sudo apt install chromium-driver`
7. `cp secrets.env.example secrets.env` and fill it in
8. `chmod +x shell_scripts/*.sh`
9. If upgrading from the pre-profiles layout: `python migrate_to_profiles.py --dry-run`, then without the flag
10. Add cron jobs (`crontab -e`):
    ```cron
    */10 * * * *   /home/pi/craigslist_alert/shell_scripts/run_scraper.sh >> /home/pi/craigslist_alert/logs/scraper.log 2>&1
    2-59/10 * * * * /home/pi/craigslist_alert/shell_scripts/upload_csv.sh  >> /home/pi/craigslist_alert/logs/git.log     2>&1
    5 7,10,16,22 * * * /home/pi/craigslist_alert/shell_scripts/run_alert.sh >> /home/pi/craigslist_alert/logs/alert.log  2>&1
    ```
    These loop over every enabled profile, so adding a person needs no cron change.

    The 10-minute cadence exists to keep the **dashboard** fresh — that's the
    only thing that benefits from it. `run_alert.sh` sends at most one digest per
    profile per day; it runs four times purely as a retry, so an outage at 07:05
    is picked up at 10:05. `last_digest_date.txt` is what stops duplicates.
11. DNS watchdog, as root (`sudo crontab -e`):
    ```cron
    */5 * * * * /home/pi/craigslist_alert/shell_scripts/dns_probe.sh
    ```

The scripts locate the repo and conda themselves, so nothing above is
path-sensitive except the cron lines. Override with `CONDA_ENV` / `CONDA_SH` if
your install differs.

---

## Adding a New Scraper

Each scraper should:
- Import shared paths from `config.py` (which resolves the active profile)
- Write rows with the same schema as `craigslist_scraper.py`
- Set `source` to the site name (e.g. `'zillow'`, `'facebook'`)
- Append to `DATA_ACTIVE`, never overwrite it
- Accept `--profile` via `config.add_profile_arg(parser)`

The alert script and dashboard are source-agnostic and need no changes.
