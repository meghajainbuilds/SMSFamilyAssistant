#!/usr/bin/env bash
# install_staging_service.sh — one-time launchd bootstrap for the STAGING
# runtime. Run this ON KAVI'S MAC (not from Megha's laptop):
#
#     ssh "$KAVI_HOST"   (from kavi-runtime/.deploy.env)
#     bash /Users/kavi/kavi-staging/scripts/install_staging_service.sh
#
# Prerequisites (also one-time, in this order):
#   1. From Megha's Mac: ./scripts/deploy_staging.sh   (populates the dir;
#      its kickstart step fails on first run because this service doesn't
#      exist yet — that's expected, continue here)
#   2. On Kavi: cd /Users/kavi/kavi-staging
#               python3 -m venv .venv && .venv/bin/pip install -e .
#   3. On Kavi: this script.
#   4. From Megha's Mac: ./scripts/deploy_staging.sh again — should now
#      kickstart cleanly and pass the /health curl.
#
# Idempotent: boots the service out first if it is already loaded.

set -euo pipefail

LABEL="com.megha.kavi-staging"
USER_ID="${USER_ID:-501}"
STAGING_DIR="${STAGING_DIR:-/Users/kavi/kavi-staging}"
PLIST_SOURCE="$STAGING_DIR/scripts/$LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

log() { printf "[install-staging] %s\n" "$*" >&2; }
fail() { printf "[install-staging] FAIL: %s\n" "$*" >&2; exit 1; }

[[ -f "$PLIST_SOURCE" ]] || fail "plist not found at $PLIST_SOURCE — run deploy_staging.sh from Megha's Mac first"
[[ -x "$STAGING_DIR/.venv/bin/kavi-runtime" ]] || fail \
    "venv entrypoint missing. Run: cd $STAGING_DIR && python3 -m venv .venv && .venv/bin/pip install -e ."

mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SOURCE" "$PLIST_DEST"
log "plist installed at $PLIST_DEST"

# Boot out a previously-loaded copy so re-installs pick up plist changes.
launchctl bootout "gui/$USER_ID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$USER_ID" "$PLIST_DEST"
launchctl kickstart -k "gui/$USER_ID/$LABEL"
log "service bootstrapped + kickstarted"

sleep 5
if curl -fsS --max-time 10 "http://127.0.0.1:8081/health" >/dev/null; then
    log "staging /health OK on port 8081 — install complete"
else
    fail "staging /health not responding on 8081. Check ~/Library/Logs/kavi-staging.err.log"
fi
