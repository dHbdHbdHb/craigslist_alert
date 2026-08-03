#!/usr/bin/env bash
# Shared setup for the cron scripts. Sourced by the others, never run directly.
#
# Works out where the repo is rather than hardcoding /home/pi/craigslist_alert,
# so the same scripts run on the Pi and on a laptop.
#
# Override with environment variables if your setup differs:
#   CONDA_ENV=craigslist   conda environment to activate
#   CONDA_SH=/path/to/conda.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Find conda: miniforge on the Pi, miniconda on most laptops.
if [[ -z "${CONDA_SH:-}" ]]; then
    for candidate in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3"; do
        if [[ -f "$candidate/etc/profile.d/conda.sh" ]]; then
            CONDA_SH="$candidate/etc/profile.d/conda.sh"
            break
        fi
    done
fi

if [[ -z "${CONDA_SH:-}" || ! -f "$CONDA_SH" ]]; then
    echo "error: couldn't find conda.sh. Set CONDA_SH=/path/to/etc/profile.d/conda.sh" >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$CONDA_SH"
conda activate "${CONDA_ENV:-craigslist}"

cd "$REPO_ROOT"

# Print the profiles to operate on: the names given as arguments, or every
# enabled profile if none were given.
resolve_profiles() {
    if [[ $# -gt 0 ]]; then
        printf '%s\n' "$@"
    else
        python -c "
from search_profile import load_enabled_profiles
for p in load_enabled_profiles(require_secrets=False):
    print(p.name)
"
    fi
}

# Run a command once per profile. Keeps going if one profile fails, so a broken
# config for one person never silently stops the other person's search, then
# exits non-zero so cron mail still flags it.
for_each_profile() {
    local script="$1"; shift
    # Read into an array the portable way — `mapfile` is bash 4+, and macOS
    # still ships bash 3.2, so this script has to work without it.
    local -a targets=()
    local line
    while IFS= read -r line; do
        [[ -n "$line" ]] && targets+=("$line")
    done < <(resolve_profiles "$@")

    if [[ ${#targets[@]} -eq 0 ]]; then
        echo "No enabled profiles — nothing to do." >&2
        return 0
    fi

    local status=0
    for name in "${targets[@]}"; do
        [[ -z "$name" ]] && continue
        echo "── ${script%.py} : ${name} ─────────────────────────────"
        if ! python "$script" --profile "$name"; then
            echo "FAILED: $script for profile '$name'" >&2
            status=1
        fi
    done
    return $status
}
