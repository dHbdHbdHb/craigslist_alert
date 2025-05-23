#!/usr/bin/env bash
set -e
# get the directory containing this script, then go up one level
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"
git pull origin main
git add craigslist_data/listings_active.csv
git commit -m "Auto-update listings: $(date '+%Y-%m-%d %H:%M:%S')"
git push origin main
