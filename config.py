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

# ----- Dashboard server -----
# URL of the Pi's web server (via Tailscale) serving analysis_dashboard.html.
# Set to None to omit the link from digest emails.
DASHBOARD_URL = "https://dhbdhbdhb.github.io/craigslist_alert/analysis_dashboard.html"  #"http://100.101.197.66:8080/analysis_dashboard.html"  #  Pi's Tailscale IP

# ----- Email -----
GMAIL_ADDRESS           = "hillsbunnell@gmail.com"
GMAIL_APP_PASSWORD      = "srsc cdxa mrte rsno"  # https://myaccount.google.com/apppasswords
DIGEST_RECIPIENT_EMAILS = [
    "Max.Drimmer@gmail.com",
    "siennarwhite@gmail.com",
    "hillsbunnell@gmail.com",
]
ALERT_RECIPIENT_EMAILS  = [
    # "Max.Drimmer@gmail.com",
    "hillsbunnell@gmail.com",
]

# ----- Map / routing -----
ORS_API_KEY               = '5b3ce3597851110001cf624809183d29fbaa46ecb0f48f56e62f89cb'  # https://account.heigit.org/manage/key
CALTRAIN_4TH_KING_COORDS  = [-122.3942, 37.7763]   # [lon, lat] 4th & King
CALTRAIN_22ND_ST_COORDS   = [-122.3925, 37.7577]   # [lon, lat] 22nd St
CALTRAIN_COORDS           = CALTRAIN_4TH_KING_COORDS  # backward compat alias
CHROMEDRIVER_PATH         = '/usr/bin/chromedriver'

# ----- Alert criteria -----
# Priority: listings matching all criteria trigger an immediate individual email
priority_neighborhoods           = {"Chill Mission", "Duboce", "NOPA/Inner Richmond", "Haight/Cole Valley", "Bernal", "Potrero Hill"}
priority_max_price               = 4000
priority_min_price               = 2800   # below this is suspiciously cheap (scam signal)
priority_min_bathrooms           = 2
priority_min_posting_age_minutes = 20     # wait for Craigslist community flagging to work
priority_scam_keywords           = [
    "email only", "email me only", "overseas", "military deployment",
    "god fearing", "god-fearing", "contact us at", "western union",
    "no calls", "no phone calls",
]

# Digest: listings outside this price range are excluded from the daily digest
digest_min_price = 2100
digest_max_price = 5500
