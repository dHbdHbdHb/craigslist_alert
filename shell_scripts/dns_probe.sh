#!/usr/bin/env bash
# DNS health probe. Runs from root crontab every 5 minutes.
# On repeated failure: restarts resolver, then NetworkManager, then reboots.
# Addresses the 04-15 outage where DNS broke for 6 days and nothing recovered.

set -u

STATE_FILE="/var/lib/dns_probe/fail_count"
LOG_FILE="/var/log/dns_probe.log"

mkdir -p "$(dirname "$STATE_FILE")"
[[ -f "$STATE_FILE" ]] || echo 0 > "$STATE_FILE"

log() { echo "[$(date -Iseconds)] $*" >> "$LOG_FILE"; }

probe_ok() {
    getent hosts sfbay.craigslist.org >/dev/null 2>&1 \
        || getent hosts google.com >/dev/null 2>&1
}

if probe_ok; then
    fails=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    if [[ "$fails" -gt 0 ]]; then
        log "DNS recovered after $fails consecutive failures"
        echo 0 > "$STATE_FILE"
    fi
    exit 0
fi

fails=$(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE_FILE"
log "DNS probe FAILED ($fails consecutive)"

case "$fails" in
    3)
        if systemctl is-active --quiet systemd-resolved; then
            log "Escalation 1: restarting systemd-resolved"
            systemctl restart systemd-resolved
        else
            log "Escalation 1: systemd-resolved not active, restarting NetworkManager"
            systemctl restart NetworkManager
        fi
        ;;
    6)
        log "Escalation 2: restarting NetworkManager"
        systemctl restart NetworkManager
        ;;
    12)
        log "Escalation 3: DNS dead ~60 min, rebooting"
        /sbin/reboot
        ;;
esac
