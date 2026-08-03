#!/usr/bin/env bash
# Send priority alerts + the daily digest for every enabled profile.
#
#   bash shell_scripts/run_alert.sh             # all enabled profiles
#   bash shell_scripts/run_alert.sh alex        # just one
#
# The digest is skipped if it already went out today — see last_digest_date.txt
# in each profile's data directory.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

for_each_profile email_alert.py "$@"
