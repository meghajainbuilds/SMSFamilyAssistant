#!/usr/bin/env bash
# deploy.sh - rsync kavi_runtime/ + tests/ + skills/ to Kavi, kickstart
# the launchd service, write the deployed git SHA, then run
# verify_deploy.sh. On verify failure, automatically roll back to the
# previous SHA and re-verify; surface the failure to stderr.
#
# Discipline rule §8: "roll-forward only on green." If a deploy fails
# verification, response is `git revert` + redeploy, not "patch on top
# of unverified state." This script automates that for the common case
# (verify fails immediately after deploy).
#
# Usage:
#     ./scripts/deploy.sh                      # deploy current local HEAD
#     ./scripts/deploy.sh <sha>                # deploy a specific SHA
#     SKIP_VERIFY=1 ./scripts/deploy.sh        # rsync only, no verify (rare)
#
# Auto-rollback flow:
#   1. Read current remote .deploy_sha as PREV_SHA before rsync
#   2. Rsync new SHA's files, kickstart, write new .deploy_sha
#   3. Run verify_deploy.sh
#   4. On failure: git checkout PREV_SHA into a temp tree, rsync those
#      files, kickstart, re-verify, exit 1 with the original failure
#      surfaced in stderr.
#
# The contract intentionally does NOT auto-rollback if verify fails on
# the FIRST EVER deploy (no PREV_SHA to revert to). In that case the
# deploy is left as-is and the operator gets a loud failure.

set -euo pipefail

source "$(dirname "$0")/load_deploy_env.sh"
require_deploy_env KAVI_HOST
KAVI_RUNTIME_DIR="${KAVI_RUNTIME_DIR:-/Users/kavi/kavi-runtime}"
LOCAL_RUNTIME_DIR="${LOCAL_RUNTIME_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SCRIPTS_DIR="$LOCAL_RUNTIME_DIR/scripts"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.megha.kavi}"
LAUNCHD_USER_ID="${LAUNCHD_USER_ID:-501}"

log() { printf "[deploy] %s\n" "$*" >&2; }
fail() { printf "[deploy] FAIL: %s\n" "$*" >&2; exit 1; }

target_sha="${1:-$(git -C "$LOCAL_RUNTIME_DIR" rev-parse HEAD)}"
log "deploying SHA: $target_sha"

prev_sha=$(ssh "$KAVI_HOST" "cat $KAVI_RUNTIME_DIR/.deploy_sha 2>/dev/null || echo NONE")
log "previous SHA: $prev_sha"

# Drain guard (added 2026-09-23). A restart mid-drain is what lost 3,090 queued
# emails on 2026-09-23. The drain is now crash-safe (queue stays on disk and the
# watchdog resumes it), but restarting still interrupts a long catch-up, so wait
# for it. FORCE_DURING_DRAIN=1 overrides (e.g. a stale marker after a crash).
DRAIN_MARKER="${DRAIN_MARKER:-/Users/kavi/HomeOS/state/drain_in_progress}"
if ssh "$KAVI_HOST" "test -e $DRAIN_MARKER"; then
    if [[ "${FORCE_DURING_DRAIN:-}" == "1" ]]; then
        log "WARNING: email catch-up in progress ($DRAIN_MARKER); FORCE_DURING_DRAIN=1, deploying anyway"
    else
        fail "email catch-up in progress on Kavi ($DRAIN_MARKER). Wait for it to finish, or FORCE_DURING_DRAIN=1 to override."
    fi
fi

# Private overlay guard (2026-09-24): every private block in the specs and
# skills must resolve before anything ships, or composer calls fail on Kavi.
"$LOCAL_RUNTIME_DIR/.venv/bin/python" "$SCRIPTS_DIR/private_overlay.py" check \
    || fail "private overlay check failed; run scripts/private_overlay.py check"

deploy_sha_to_kavi() {
    local sha="$1"
    local source_root="$2"
    log "rsyncing $source_root -> $KAVI_HOST:$KAVI_RUNTIME_DIR/ (SHA $sha)"
    rsync -av --delete \
        "$source_root/kavi_runtime/" \
        "$KAVI_HOST:$KAVI_RUNTIME_DIR/kavi_runtime/"
    rsync -av --delete \
        "$source_root/tests/" \
        "$KAVI_HOST:$KAVI_RUNTIME_DIR/tests/"
    # skills/ — historically forgotten by rsync (miss #4, 2026-05-08
    # post-mortem). The contract is "kavi_runtime/ + tests/ + skills/ +
    # capabilities/" and the verifier asserts hash-match in step 2.
    rsync -av --delete \
        "$source_root/skills/" \
        "$KAVI_HOST:$KAVI_RUNTIME_DIR/skills/"
    # capabilities/ — added 2026-05-27 (spec-IS-the-runtime collapse).
    # The persona spec (capabilities/kavi-persona.md) is now LOADED by
    # the runtime at compose time via kavi_runtime/persona_loader.py.
    # Source lives at the repo root, so the deploy worktree is
    # $LOCAL_RUNTIME_DIR/../capabilities/. Without this rsync the loader
    # raises FileNotFoundError and composer calls fail loudly on Kavi.
    # --exclude runtime data (2026-09-23): the handler wrote the pre-filter
    # shadow log under capabilities/runtime_metrics/ and --delete wiped it on
    # every deploy. The path is fixed; the exclude is the safety net.
    rsync -av --delete \
        --exclude='runtime_metrics/' --exclude='*.jsonl' \
        "$source_root/../capabilities/" \
        "$KAVI_HOST:$KAVI_RUNTIME_DIR/capabilities/"
    # config.yaml — added 2026-05-28 after the drift-alarm deploy failed
    # because new paths.* keys (kavi_persona_md, security_baseline_md,
    # startup_marker) had been added to config.yaml over the prior
    # three weeks but never shipped to Kavi. The startup probe was strict
    # about the keys being present; the loaders had silent fallbacks that
    # masked the gap. Add config.yaml to the rsync list so every config
    # change reaches the runtime on the next deploy.
    # 2026-09-24: config.yaml is private and gitignored (public-repo prep),
    # so it is always shipped from the live local file, never from a
    # rollback checkout (which no longer contains it).
    rsync -av \
        "$LOCAL_RUNTIME_DIR/config.yaml" \
        "$KAVI_HOST:$KAVI_RUNTIME_DIR/config.yaml"
    # private/ (2026-09-24, public-repo step 2): the gitignored overlay that
    # swaps real family examples into the public specs and skills at load
    # time (kavi_runtime/private_overlay.py). Like config.yaml, always shipped
    # from the live local copy. Without it every composer call raises.
    rsync -av --delete \
        "$LOCAL_RUNTIME_DIR/../private/overlays/" \
        "$KAVI_HOST:$KAVI_RUNTIME_DIR/private/overlays/"
    # scripts/ — added 2026-05-31. Phase 3 (one state file per concept)
    # ships a migration script that runs on boot. Without scripts/ in
    # the rsync, the migration never reaches Kavi and the on-boot
    # invocation fails. Excludes __pycache__ and ad-hoc local artifacts.
    rsync -av --delete \
        --exclude='__pycache__' \
        "$source_root/scripts/" \
        "$KAVI_HOST:$KAVI_RUNTIME_DIR/scripts/"
    ssh "$KAVI_HOST" "find $KAVI_RUNTIME_DIR -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true"
    ssh "$KAVI_HOST" "echo $sha > $KAVI_RUNTIME_DIR/.deploy_sha"
    ssh "$KAVI_HOST" "launchctl kickstart -k gui/$LAUNCHD_USER_ID/$LAUNCHD_LABEL"
    sleep 3
}

# Forward path: rsync from current local checkout (assumes target_sha == HEAD).
local_head=$(git -C "$LOCAL_RUNTIME_DIR" rev-parse HEAD)
if [[ "$target_sha" != "$local_head" ]]; then
    fail "deploy.sh requires target SHA to equal local HEAD; got target=$target_sha local=$local_head"
fi

deploy_sha_to_kavi "$target_sha" "$LOCAL_RUNTIME_DIR"

if [[ "${SKIP_VERIFY:-}" == "1" ]]; then
    log "SKIP_VERIFY=1 — skipping post-deploy verify (deployed but not asserted live)"
    exit 0
fi

if "$SCRIPTS_DIR/verify_deploy.sh"; then
    log "deploy + verify OK at SHA $target_sha"
    exit 0
fi

# ---- Rollback --------------------------------------------------------------

log "VERIFY FAILED — initiating auto-rollback"
if [[ "$prev_sha" == "NONE" || -z "$prev_sha" ]]; then
    log "no previous SHA recorded; cannot auto-rollback"
    fail "verify failed AND no rollback target — manual intervention required"
fi

# Build a worktree at PREV_SHA in a temp dir and rsync from there.
tmp_worktree=$(mktemp -d -t kavi_rollback)
trap 'rm -rf "$tmp_worktree"' EXIT
log "rolling back to $prev_sha via temp worktree $tmp_worktree"
git -C "$LOCAL_RUNTIME_DIR" worktree add --detach "$tmp_worktree" "$prev_sha" >/dev/null

# In the worktree, kavi-runtime/ is the right subdirectory.
deploy_sha_to_kavi "$prev_sha" "$tmp_worktree/kavi-runtime"

# Verify the rollback against the rollback tree, not local HEAD (2026-09-24:
# comparing against HEAD meant a rollback could never pass, so every
# rollback cried "manual intervention required"). config.yaml is private
# and not in the worktree, so the health check reads the live local one.
if LOCAL_RUNTIME_DIR="$tmp_worktree/kavi-runtime" \
        HEALTH_CONFIG="$LOCAL_RUNTIME_DIR/config.yaml" \
        "$SCRIPTS_DIR/verify_deploy.sh"; then
    git -C "$LOCAL_RUNTIME_DIR" worktree remove --force "$tmp_worktree" >/dev/null
    fail "verify failed at $target_sha; rolled back to $prev_sha and verify-passed there. Kavi is safe on $prev_sha. Diagnose the forward failure above."
fi
git -C "$LOCAL_RUNTIME_DIR" worktree remove --force "$tmp_worktree" >/dev/null
fail "verify failed at $target_sha AND rollback to $prev_sha also failed verify — manual intervention required"
