#!/usr/bin/env bash
set -e
# get the directory containing this script, then go up one level
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

source /home/pi/miniforge3/etc/profile.d/conda.sh
conda activate craigslist

cd "$REPO_ROOT"
git stash
git pull --rebase origin main
git stash pop

python /home/pi/craigslist_alert/analyze_listings.py
git add craigslist_data/listings_active.csv craigslist_data/listings_archive.csv analysis_dashboard.html email_map.png
git diff --cached --quiet || git commit -m "Auto-update listings: $(date '+%Y-%m-%d %H:%M:%S')"
git push origin main
