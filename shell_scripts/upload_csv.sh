#!/usr/bin/env bash
# Rebuild every enabled profile's dashboard, then commit and push the data.
#
#   bash shell_scripts/upload_csv.sh            # all enabled profiles
#   bash shell_scripts/upload_csv.sh alex       # rebuild one, still commits all
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

build_status=0
for_each_profile analyze_listings.py "$@" || build_status=$?

# Commit whatever exists, even if one profile's dashboard failed to build —
# losing a day of scraped listings is worse than pushing a stale dashboard.
git add data dashboards
git diff --cached --quiet || git commit -m "Auto-update listings: $(date '+%Y-%m-%d %H:%M:%S')"

# --rebase replays our commit on top if the other machine pushed first.
git pull --rebase origin main
git push origin main

exit $build_status
