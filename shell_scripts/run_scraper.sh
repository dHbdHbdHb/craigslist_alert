#!/usr/bin/env bash
# Scrape Craigslist for every enabled profile.
#
#   bash shell_scripts/run_scraper.sh           # all enabled profiles
#   bash shell_scripts/run_scraper.sh alex      # just one
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

for_each_profile craigslist_scraper.py "$@"
