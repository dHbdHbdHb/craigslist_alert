"""
config.py — resolves the active profile and re-exports it as module-level names.

This used to be the file you edited to configure the app. It isn't anymore:
everything personal now lives in profiles/<name>.toml, and every secret lives
in secrets.env. Both are described in SETUP.md. This module just picks which
profile is active and flattens it into the names the rest of the code imports.

Which profile is active, in priority order:

    1. --profile NAME on the command line
    2. the HOUSING_PROFILE environment variable
    3. the single enabled profile, if there's exactly one

Because the modules below use `from config import X` at import time, the profile
has to be resolved when this module is first imported — which is why the
--profile flag is read straight from sys.argv here rather than by argparse.
The entry-point scripts still register the flag (via add_profile_arg) so that it
shows up in --help and doesn't trip "unrecognized argument".
"""

import sys

from search_profile import (  # noqa: F401  (re-exported for callers)
    MAX_ACTIVE_ROWS,
    Profile,
    ProfileError,
    add_profile_arg,
    load_enabled_profiles,
    load_profile,
)


def _profile_from_argv() -> str | None:
    """Read --profile NAME or --profile=NAME from argv without consuming it."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1]
    return None


PROFILE: Profile = load_profile(_profile_from_argv())
PROFILE.ensure_dirs()

# ── Identity ──────────────────────────────────────────────────────────────────
PROFILE_NAME = PROFILE.name
DISPLAY_NAME = PROFILE.display_name

# ── File paths (per profile) ──────────────────────────────────────────────────
DATA_ACTIVE      = str(PROFILE.active_csv)
DATA_ARCHIVE     = str(PROFILE.archive_csv)
LAST_DIGEST_FILE = str(PROFILE.last_digest_file)
BIKE_ROUTES_PATH = str(PROFILE.bike_routes_json)
BART_ROUTES_PATH = str(PROFILE.bart_routes_json)
DASHBOARD_HTML   = PROFILE.dashboard_html

# ── Search ────────────────────────────────────────────────────────────────────
SEARCH_URL   = PROFILE.search_url
SEARCH_CITY  = PROFILE.city
max_price    = PROFILE.max_price
min_bedrooms = PROFILE.min_bedrooms

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_URL   = PROFILE.dashboard_url or None
MAP_CENTER      = PROFILE.map_center
SHOW_HISTORICAL = PROFILE.show_historical

# ── Email ─────────────────────────────────────────────────────────────────────
GMAIL_ADDRESS           = PROFILE.gmail_address
GMAIL_APP_PASSWORD      = PROFILE.gmail_app_password
DIGEST_RECIPIENT_EMAILS = PROFILE.digest_to
ALERT_RECIPIENT_EMAILS  = PROFILE.priority_to

# ── Map / routing ─────────────────────────────────────────────────────────────
ORS_API_KEY       = PROFILE.ors_api_key
CALTRAIN_STATIONS = [(n, c) for n, c in PROFILE.caltrain_stations]
BART_STATIONS     = [(n, c) for n, c in PROFILE.bart_stations]
CHROMEDRIVER_PATH = "/usr/bin/chromedriver"

# ── Alert criteria ────────────────────────────────────────────────────────────
INCLUDE_NEIGHBORHOODS            = PROFILE.include_neighborhoods
priority_neighborhoods           = PROFILE.priority_neighborhoods
priority_max_price               = PROFILE.priority_max_price
priority_min_price               = PROFILE.priority_min_price
priority_min_bathrooms           = PROFILE.priority_min_bathrooms
priority_min_posting_age_minutes = PROFILE.priority_min_posting_age_minutes
priority_scam_keywords           = PROFILE.priority_scam_keywords
digest_min_price                 = PROFILE.digest_min_price
digest_max_price                 = PROFILE.digest_max_price
