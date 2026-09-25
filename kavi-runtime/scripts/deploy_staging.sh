#!/usr/bin/env bash
# deploy_staging.sh - rsync the runtime to the STAGING instance on Kavi's
# Mac (/Users/kavi/kavi-staging/, port 8081, launchd label
# com.megha.kavi-staging), ship config-staging.yaml AS the staging
# config.yaml, write .deploy_sha, kickstart, then curl /health.
#
# Staging is the LLM-replay sandbox for the capability-build pipeline
# (capabilities/BUILD_PIPELINE.md): synthetic compose/verify routes only,
# outbound hard-disabled, no Graph subscriptions, no webhook processing.
# It is DISPOSABLE — no auto-rollback. On verify failure: exit 1, fix
# forward or redeploy. Production deploys keep their rollback contract in
# scripts/deploy.sh; do not copy this looser posture back there.
#
# One-time setup on Kavi (see scripts/install_staging_service.sh):
#   1. Run this script once to populate /Users/kavi/kavi-staging/.
#   2. ssh "$KAVI_HOST"   (from kavi-runtime/.deploy.env)
#      cd /Users/kavi/kavi-staging
#      python3 -m venv .venv && .venv/bin/pip install -e .
#   3. bash scripts/install_staging_service.sh   (on Kavi)
#
# Usage:
#     ./scripts/deploy_staging.sh          # deploy current local HEAD

set -euo pipefail

source "$(dirname "$0")/load_deploy_env.sh"
require_deploy_env KAVI_HOST KAVI_ADDR
KAVI_STAGING_DIR="${KAVI_STAGING_DIR:-/Users/kavi/kavi-staging}"
KAVI_STAGING_STATE_ROOT="${KAVI_STAGING_STATE_ROOT:-/Users/kavi/HomeOS-staging}"
LOCAL_RUNTIME_DIR="${LOCAL_RUNTIME_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.megha.kavi-staging}"
LAUNCHD_USER_ID="${LAUNCHD_USER_ID:-501}"
STAGING_HEALTH_URL="${STAGING_HEALTH_URL:-http://$KAVI_ADDR:8081/health}"

log() { printf "[deploy-staging] %s\n" "$*" >&2; }
fail() { printf "[deploy-staging] FAIL: %s\n" "$*" >&2; exit 1; }

target_sha=$(git -C "$LOCAL_RUNTIME_DIR" rev-parse HEAD)
log "deploying SHA: $target_sha -> $KAVI_HOST:$KAVI_STAGING_DIR (STAGING)"

# Sandbox state tree. The staging config's paths.* all live under
# $KAVI_STAGING_STATE_ROOT so synthetic-replay log rows never touch
# production HomeOS state.
ssh "$KAVI_HOST" "mkdir -p \
    $KAVI_STAGING_DIR \
    $KAVI_STAGING_STATE_ROOT/state \
    $KAVI_STAGING_STATE_ROOT/metrics \
    $KAVI_STAGING_STATE_ROOT/evals/inbox-to-task \
    $KAVI_STAGING_STATE_ROOT/evals/kavi-persona \
    $KAVI_STAGING_STATE_ROOT/evals/kavi-coordinates \
    $KAVI_STAGING_STATE_ROOT/evals/traces"

# Same file sets as scripts/deploy.sh (kavi_runtime/ + tests/ + skills/ +
# capabilities/ + scripts/), different destination root.
rsync -av --delete \
    "$LOCAL_RUNTIME_DIR/kavi_runtime/" \
    "$KAVI_HOST:$KAVI_STAGING_DIR/kavi_runtime/"
rsync -av --delete \
    "$LOCAL_RUNTIME_DIR/tests/" \
    "$KAVI_HOST:$KAVI_STAGING_DIR/tests/"
rsync -av --delete \
    "$LOCAL_RUNTIME_DIR/skills/" \
    "$KAVI_HOST:$KAVI_STAGING_DIR/skills/"
rsync -av --delete \
    --exclude='runtime_metrics/' --exclude='*.jsonl' \
    "$LOCAL_RUNTIME_DIR/../capabilities/" \
    "$KAVI_HOST:$KAVI_STAGING_DIR/capabilities/"
rsync -av --delete \
    --exclude='__pycache__' \
    "$LOCAL_RUNTIME_DIR/scripts/" \
    "$KAVI_HOST:$KAVI_STAGING_DIR/scripts/"
# private/ (2026-09-24, public-repo step 2): the gitignored overlay that
# swaps real family examples into the public specs and skills at load
# time (kavi_runtime/private_overlay.py). Like config.yaml, always shipped
# from the live local copy. Without it every composer call raises.
rsync -av --delete \
        "$LOCAL_RUNTIME_DIR/../private/overlays/" \
        "$KAVI_HOST:$KAVI_STAGING_DIR/private/overlays/"
# pyproject.toml — staging has its own venv (`pip install -e .`); ship the
# project file so the editable install resolves.
rsync -av \
    "$LOCAL_RUNTIME_DIR/pyproject.toml" \
    "$KAVI_HOST:$KAVI_STAGING_DIR/pyproject.toml"
# THE staging switch: config-staging.yaml ships AS the instance's
# config.yaml. main.py reads <working-dir>/config.yaml, so the staging
# service boots with staging_mode=true, port 8081, sandboxed paths.
rsync -av \
    "$LOCAL_RUNTIME_DIR/config-staging.yaml" \
    "$KAVI_HOST:$KAVI_STAGING_DIR/config.yaml"
# Read-only spec inputs the composers load at compose time
# (paths.household_md / paths.inbox_to_task_md in config-staging.yaml
# point into the sandbox tree). Discovered on the first staging boot:
# /synthetic/verify 500'd with FileNotFoundError on household.md.
ssh "$KAVI_HOST" "mkdir -p $KAVI_STAGING_STATE_ROOT/capabilities"
rsync -av \
    "$LOCAL_RUNTIME_DIR/../household.md" \
    "$KAVI_HOST:$KAVI_STAGING_STATE_ROOT/household.md"
rsync -av \
    "$LOCAL_RUNTIME_DIR/../capabilities/inbox-to-task.md" \
    "$KAVI_HOST:$KAVI_STAGING_STATE_ROOT/capabilities/inbox-to-task.md"

ssh "$KAVI_HOST" "find $KAVI_STAGING_DIR -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true"
ssh "$KAVI_HOST" "echo $target_sha > $KAVI_STAGING_DIR/.deploy_sha"
ssh "$KAVI_HOST" "launchctl kickstart -k gui/$LAUNCHD_USER_ID/$LAUNCHD_LABEL" \
    || fail "kickstart failed — is the service installed? Run scripts/install_staging_service.sh ON Kavi first."
sleep 5

log "verifying staging health at $STAGING_HEALTH_URL"
if curl -fsS --max-time 10 "$STAGING_HEALTH_URL" >/dev/null; then
    log "staging deploy OK at SHA $target_sha ($STAGING_HEALTH_URL healthy)"
    exit 0
fi
fail "staging /health did not return 200. Check ~/Library/Logs/kavi-staging.err.log on Kavi. Staging is disposable: fix forward and redeploy."
