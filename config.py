"""
config.py — shared configuration for all scrapers and the alert script.

To add a new scraper: import what you need from here and set source='yoursite'
on each listing row. The alert script is source-agnostic and requires no changes.
"""

import os

# ----- File paths -----
BASE_DIR         = os.path.expanduser("~/craigslist_alert")
DATA_ACTIVE      = os.path.join(BASE_DIR, "craigslist_data", "listings_active.csv")
DATA_ARCHIVE     = os.path.join(BASE_DIR, "craigslist_data", "listings_archive.csv")
LAST_DIGEST_FILE = os.path.join(BASE_DIR, "last_digest_date.txt")
MAX_ACTIVE_ROWS  = 1000

# ----- Email -----
GMAIL_ADDRESS           = "hillsbunnell@gmail.com"
GMAIL_APP_PASSWORD      = "eknq yzlh jkop vkdg"  # https://myaccount.google.com/apppasswords
DIGEST_RECIPIENT_EMAILS = [
    # "Max.Drimmer@gmail.com",
    "hillsbunnell@gmail.com",
]
ALERT_RECIPIENT_EMAILS  = [
    # "Max.Drimmer@gmail.com",
    "hillsbunnell@gmail.com",
]

# ----- Map / routing -----
ORS_API_KEY       = '5b3ce3597851110001cf624809183d29fbaa46ecb0f48f56e62f89cb'  # https://account.heigit.org/manage/key
CALTRAIN_COORDS   = [-122.3942, 37.7763]  # [lon, lat]
CHROMEDRIVER_PATH = '/usr/bin/chromedriver'

# ----- Alert criteria -----
# Priority: listings matching all three criteria trigger an immediate individual email
priority_neighborhoods = {"Chill Mission", "Duboce", "NOPA/Inner Richmond", "Haight/Cole Valley", "Bernal"}
priority_max_price     = 4000
priority_min_bathrooms = 2

# Digest: all unalerted listings under this price are included in the daily digest
digest_max_price = 5500
