#!/usr/bin/env bash
# Rebuild every enabled profile's dashboard, then commit and push the data.
#
#   bash shell_scripts/upload_csv.sh            # all enabled profiles
#   bash shell_scripts/upload_csv.sh alex       # rebuild one, still commits all
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

build_status=0
for_each_profile analyze_listings.py "$@" || build_status=$?

# A rebase left in progress by an earlier run brings everything to a halt: the
# repo sits on a detached HEAD, every later run commits onto it, and every push
# is rejected as non-fast-forward until a human notices. On 2026-08-05 that ran
# for an hour and 14 rejected pushes before anyone looked.
#
# Aborting is safe here because nothing in this script's own work is inside the
# rebase — the commit above has already been made, and abort returns to it.
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    echo "warning: a rebase was left in progress by an earlier run — aborting it"
    git rebase --abort || rm -rf .git/rebase-merge .git/rebase-apply
fi

# Commit whatever exists, even if one profile's dashboard failed to build —
# losing a day of scraped listings is worse than pushing a stale dashboard.
git add data dashboards
git diff --cached --quiet || git commit -m "Auto-update listings: $(date '+%Y-%m-%d %H:%M:%S')"

# --rebase replays our commit on top if the other machine pushed first.
#
# -X theirs resolves content conflicts in favour of the commit being replayed,
# i.e. this machine's. It exists for dashboards/*.html: both machines rebuild
# the same generated file, so a conflict there is guaranteed whenever both have
# pushed, and it is also meaningless — the next run overwrites the file anyway.
# Without this the rebase stops dead and the branch wedges (see above).
#
# The CSVs are unaffected: .gitattributes gives them merge=union, and a
# per-path driver takes precedence over a strategy option, so listing rows are
# still unioned rather than one side winning.
git pull --rebase -X theirs origin main
git push origin main

exit $build_status
