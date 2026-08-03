#!/usr/bin/env bash
# provision_pi.sh — rebuild the whole stack on a freshly-flashed Raspberry Pi.
#
# Run this after flashing a new card and cloning the repo:
#
#   git clone https://github.com/dHbdHbdHb/craigslist_alert.git ~/craigslist_alert
#   cd ~/craigslist_alert && bash provision_pi.sh
#
# Idempotent — safe to re-run. It skips anything already done, so if a step
# fails you can fix it and run the whole thing again.
#
# It deliberately does NOT install cron jobs or write secrets for you. It prints
# exactly what to do for both at the end, because those are the two steps worth
# looking at with your own eyes.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_ENV="${CONDA_ENV:-craigslist}"
MINIFORGE_DIR="$HOME/miniforge3"

ok()   { echo "  ✓ $*"; }
info() { echo "  · $*"; }
warn() { echo "  ! $*" >&2; }
step() { echo; echo "── $* ─────────────────────────────────────────"; }

fail_count=0
fail() { echo "  ✗ $*" >&2; fail_count=$((fail_count + 1)); }

step "1. System packages"
# chromium-driver is used by the image-fetching helpers; git and curl for the rest.
missing_pkgs=()
for pkg in git curl chromium-driver; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        ok "$pkg already installed"
    else
        missing_pkgs+=("$pkg")
    fi
done

NEEDS_SUDO_STEPS=()
if [[ ${#missing_pkgs[@]} -eq 0 ]]; then
    :
elif sudo -n true 2>/dev/null; then
    info "installing: ${missing_pkgs[*]}"
    sudo apt-get update -qq && sudo apt-get install -y "${missing_pkgs[@]}" \
        && ok "installed ${missing_pkgs[*]}" \
        || fail "apt install failed for: ${missing_pkgs[*]}"
else
    # No passwordless sudo — which is the norm over a non-interactive SSH
    # session. Rather than hanging on an invisible password prompt, note what
    # needs doing and carry on with everything that doesn't need root.
    warn "sudo needs a password — skipping apt for now (nothing else is blocked)"
    NEEDS_SUDO_STEPS+=("sudo apt-get update && sudo apt-get install -y ${missing_pkgs[*]}")
fi

step "2. Miniforge"
if [[ -d "$MINIFORGE_DIR" ]]; then
    ok "miniforge already at $MINIFORGE_DIR"
else
    info "downloading miniforge for $(uname -m)…"
    installer="/tmp/miniforge.sh"
    if curl -fsSL -o "$installer" \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-$(uname -m).sh"; then
        bash "$installer" -b -p "$MINIFORGE_DIR" && ok "miniforge installed" \
            || fail "miniforge installer failed"
        rm -f "$installer"
    else
        fail "could not download miniforge"
    fi
fi

CONDA_SH="$MINIFORGE_DIR/etc/profile.d/conda.sh"
if [[ ! -f "$CONDA_SH" ]]; then
    fail "conda.sh not found at $CONDA_SH — cannot continue"
    echo; echo "Provisioning aborted with $fail_count failure(s)." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$CONDA_SH"

step "3. Conda environment '$CONDA_ENV'"
if conda env list | grep -qE "^${CONDA_ENV}\s"; then
    ok "environment already exists"
else
    info "creating from environment.yml (this takes a while on a Pi)…"
    conda env create -f "$REPO_ROOT/environment.yml" -n "$CONDA_ENV" \
        && ok "environment created" || fail "conda env create failed"
fi

conda activate "$CONDA_ENV" || fail "could not activate $CONDA_ENV"

# environment.yml predates the transit-times feature and omits this one.
if python -c "import openrouteservice" 2>/dev/null; then
    ok "openrouteservice present"
else
    info "installing openrouteservice (missing from environment.yml)…"
    pip install --quiet openrouteservice && ok "openrouteservice installed" \
        || fail "pip install openrouteservice failed"
fi

step "4. Credentials"
if [[ -f "$REPO_ROOT/secrets.env" ]]; then
    ok "secrets.env exists"
else
    cp "$REPO_ROOT/secrets.env.example" "$REPO_ROOT/secrets.env"
    warn "created secrets.env from the template — you must fill it in before"
    warn "  anything will send email. See step A below."
fi

step "5. Data migration"
# No-op unless an old single-user craigslist_data/ directory is present.
if [[ -d "$REPO_ROOT/craigslist_data" ]]; then
    info "legacy craigslist_data/ found — migrating to per-profile layout"
    python "$REPO_ROOT/migrate_to_profiles.py" || fail "migration failed"
else
    ok "already on the per-profile layout"
fi

step "6. Permissions"
chmod +x "$REPO_ROOT"/shell_scripts/*.sh && ok "shell scripts executable"
mkdir -p "$REPO_ROOT/logs" && ok "logs/ ready"

step "7. Verify"
if python "$REPO_ROOT/search_profile.py"; then
    ok "profiles valid"
else
    warn "profile check failed — fix the reported issue, then re-run this script"
fi

# ── What's left ───────────────────────────────────────────────────────────────
if [[ ${#NEEDS_SUDO_STEPS[@]} -gt 0 ]]; then
    cat <<EOF

══════════════════════════════════════════════════════════════════════
  Needs a password — run these yourself
══════════════════════════════════════════════════════════════════════

EOF
    for cmd in "${NEEDS_SUDO_STEPS[@]}"; do
        echo "   $cmd"
    done
fi

cat <<EOF

══════════════════════════════════════════════════════════════════════
  Two manual steps remain
══════════════════════════════════════════════════════════════════════

A. Fill in secrets.env
   nano $REPO_ROOT/secrets.env

   Needs GMAIL_ADDRESS, GMAIL_APP_PASSWORD (an app password, not your
   account password: https://myaccount.google.com/apppasswords) and
   ORS_API_KEY (https://account.heigit.org/manage/key).

B. Install the cron jobs
   crontab -e     # then paste:

*/10 * * * *      $REPO_ROOT/shell_scripts/run_scraper.sh >> $REPO_ROOT/logs/scraper.log 2>&1
2-59/10 * * * *   $REPO_ROOT/shell_scripts/upload_csv.sh  >> $REPO_ROOT/logs/git.log     2>&1
5 7,10,16,22 * * * $REPO_ROOT/shell_scripts/run_alert.sh  >> $REPO_ROOT/logs/alert.log   2>&1

   sudo crontab -e   # then paste the DNS watchdog:

*/5 * * * * $REPO_ROOT/shell_scripts/dns_probe.sh

   These loop over every enabled profile, so adding a person later needs
   no cron change.

Then smoke-test the whole pipeline by hand:

   bash shell_scripts/run_scraper.sh
   python email_alert.py --dry-run --profile <name>

EOF

if [[ $fail_count -gt 0 ]]; then
    echo "Finished with $fail_count failure(s) — see the ✗ lines above." >&2
    exit 1
fi
echo "Provisioning completed cleanly."
