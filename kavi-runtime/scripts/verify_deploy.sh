#!/usr/bin/env bash
# verify_deploy.sh - end-to-end deploy verification harness for kavi-runtime.
#
# Discipline rule §5: "/health is liveness, not correctness." A 200 OK on
# /health proves the process is up; it does not prove the user-visible
# flow works. This script asserts both.
#
# Steps:
#   1. SHA match: local HEAD git SHA == remote /Users/kavi/kavi-runtime/.deploy_sha
#   2. Hash match: recursive sha256 over kavi_runtime/, tests/, skills/,
#      capabilities/ on local vs Kavi. Skill files cannot be silently
#      absent (miss #4 from the 2026-05-08 post-mortem); capabilities/
#      cannot be silently absent either (2026-05-27 spec-collapse —
#      persona_loader reads capabilities/kavi-persona.md at compose time).
#   3. /health {"status":"ok"} via SSH, or "degraded" where every violation
#      is on an account listed, with an unexpired date, under
#      deploy.accepted_health_violations in config.yaml (2026-09-24: a known,
#      accepted outage such as a disconnected mailbox must not block every
#      deploy, but any other violation still does).
#   4. Smoke fixture: POST a neutral-text BlueBubbles inbound to the
#      runtime's webhook endpoint via SSH on Kavi (loopback, no public
#      surface). Wait up to 30s for an outbound row to appear in
#      eval-persona-outbound-judgments.jsonl tagged with the smoke marker.
#
# Smoke design rationale (kept here so future maintainers don't relax
# the safety bar): the Investment-1 happy-path fixture mark-done flow
# is mutating — it would mark real Elders' Tea tasks done in MS To Do
# every deploy. That's unacceptable. Instead we POST a NEUTRAL inbound
# whose text contains a unique marker; the runtime classifier returns
# has_action=false; the conversational composer runs; an outbound row
# is written; no MS To Do state changes. This still exercises the full
# chain: BlueBubbles webhook -> classifier -> composer -> outbound log
# -> send wrapper. The action layer's correctness is covered by the
# regression library + live-LLM snapshots in Investment 3, not here.
#
# On any step failure: prints the failure, exits 1. The wrapping
# deploy.sh treats exit 1 as "auto-rollback to previous SHA."

set -euo pipefail

source "$(dirname "$0")/load_deploy_env.sh"
require_deploy_env KAVI_HOST KAVI_SMOKE_HANDLE
KAVI_RUNTIME_DIR="${KAVI_RUNTIME_DIR:-/Users/kavi/kavi-runtime}"
LOCAL_RUNTIME_DIR="${LOCAL_RUNTIME_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SMOKE_TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-30}"

log() { printf "[verify_deploy] %s\n" "$*" >&2; }
fail() { printf "[verify_deploy] FAIL: %s\n" "$*" >&2; exit 1; }

# ---- Step 1: SHA match -----------------------------------------------------

local_sha=$(git -C "$LOCAL_RUNTIME_DIR" rev-parse HEAD)
log "local SHA: $local_sha"

remote_sha=$(ssh "$KAVI_HOST" "cat $KAVI_RUNTIME_DIR/.deploy_sha 2>/dev/null || echo MISSING")
log "remote SHA: $remote_sha"

if [[ "$remote_sha" == "MISSING" ]]; then
    fail ".deploy_sha missing on Kavi — deploy.sh must write it after each rsync"
fi
if [[ "$local_sha" != "$remote_sha" ]]; then
    fail "SHA mismatch: local=$local_sha remote=$remote_sha"
fi
log "step 1 OK: SHA match"

# ---- Step 2: Hash match per directory --------------------------------------
#
# `find ... -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256`
# yields a single hash over the file-list-with-content. Same recipe runs on
# both sides; if any file differs, hashes differ.

# Runtime data the deploy rsync excludes from capabilities/ (and so never
# deletes on Kavi) must be excluded from the hash too, or a leftover data
# file fails every verify (2026-09-24).
extra_excludes() {
    if [[ "$1" == "capabilities" ]]; then
        echo "! -path '*/runtime_metrics/*' ! -name '*.jsonl'"
    fi
}

hash_local() {
    local root="$1"
    local dir="$2"
    (cd "$root" && eval find "$dir" -type f $(extra_excludes "$dir") \
        ! -name "'*.pyc'" \
        ! -path "'*/__pycache__/*'" \
        ! -name "'.DS_Store'" \
        -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 shasum -a 256 \
      | shasum -a 256 \
      | awk '{print $1}')
}

hash_remote() {
    local dir="$1"
    ssh "$KAVI_HOST" "cd $KAVI_RUNTIME_DIR && find $dir -type f $(extra_excludes "$dir") \
        ! -name '*.pyc' \
        ! -path '*/__pycache__/*' \
        ! -name '.DS_Store' \
        -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 shasum -a 256 \
      | shasum -a 256 \
      | awk '{print \$1}'"
}

# Per-dir local source root: capabilities/ lives at the REPO ROOT (parent
# of kavi-runtime/), while kavi_runtime/, tests/, skills/ live inside
# kavi-runtime/. On Kavi everything lands under $KAVI_RUNTIME_DIR/<dir>/
# by deploy.sh, so the remote path shape is uniform.
REPO_ROOT="$(cd "$LOCAL_RUNTIME_DIR/.." && pwd)"
for dir in kavi_runtime tests skills capabilities; do
    if [[ "$dir" == "capabilities" ]]; then
        local_root="$REPO_ROOT"
    else
        local_root="$LOCAL_RUNTIME_DIR"
    fi
    lh=$(hash_local "$local_root" "$dir")
    rh=$(hash_remote "$dir")
    if [[ "$lh" != "$rh" ]]; then
        log "$dir hash diverges:"
        log "  local:  $lh"
        log "  remote: $rh"
        log "diff (file lists):"
        diff <(cd "$local_root" && eval find "$dir" -type f $(extra_excludes "$dir") \
                  ! -name "'*.pyc'" ! -path "'*/__pycache__/*'" ! -name "'.DS_Store'" \
                  | LC_ALL=C sort) \
             <(ssh "$KAVI_HOST" "cd $KAVI_RUNTIME_DIR && find $dir -type f $(extra_excludes "$dir") \
                  ! -name '*.pyc' ! -path '*/__pycache__/*' ! -name '.DS_Store' \
                  | LC_ALL=C sort") \
          || true
        fail "hash mismatch in $dir"
    fi
    log "step 2 OK: $dir hash match ($lh)"
done

# ---- Step 3: /health -------------------------------------------------------

# No -f: a degraded runtime answers 503 with a JSON body we need to read.
health=$(ssh "$KAVI_HOST" "curl -sS --max-time 5 http://127.0.0.1:8080/health" || echo "FAIL")
if [[ "$health" == "FAIL" ]]; then
    fail "/health unreachable"
fi
health_verdict=$(HEALTH="$health" CONFIG="${HEALTH_CONFIG:-$LOCAL_RUNTIME_DIR/config.yaml}" python3 - <<'PY'
import datetime, json, os, re
try:
    h = json.loads(os.environ["HEALTH"])
except Exception:
    print("bad-json"); raise SystemExit
if h.get("status") == "ok":
    print("ok"); raise SystemExit
# Minimal parse of deploy.accepted_health_violations (list of
# {account, until}) so this runs with system python3 (no PyYAML).
accepted, today = {}, datetime.date.today().isoformat()
try:
    text = open(os.environ["CONFIG"]).read()
    block = re.search(r"^deploy:\n((?:[ \t].*\n|\n)*)", text, re.M)
    if block:
        for m in re.finditer(r"account:\s*\"?([^\s\"]+)\"?\s*\n\s*until:\s*\"?(\d{4}-\d{2}-\d{2})", block.group(1)):
            accepted[m.group(1).lower()] = m.group(2)
except OSError:
    pass
bad = [v for v in h.get("violations") or []
       if accepted.get((v.get("account") or "").lower(), "") < today]
print("ok-accepted" if h.get("violations") and not bad else "blocked: " + json.dumps(bad or h))
PY
)
case "$health_verdict" in
    ok) log "step 3 OK: /health returns ok" ;;
    ok-accepted) log "step 3 OK: /health degraded only by accepted violations (deploy.accepted_health_violations): $health" ;;
    *) fail "/health not ok: $health_verdict" ;;
esac

# ---- Step 4: smoke fixture --------------------------------------------------
#
# Neutral-text BlueBubbles webhook payload. Marker is a uuid we then grep
# for in /evals/kavi-persona/recent. The marker lives in the inbound text
# so the runtime's inbound logger captures it as triggered_by; the
# downstream outbound row's `triggered_by` field is the join key. We
# don't need exact response contents — proving a row landed within the
# timeout proves the chain is alive.

smoke_marker="verify_deploy_smoke_$(date -u +%Y%m%dT%H%M%SZ)_$$"
# The unique suffix (timestamp + pid) is what Kavi echoes back. After the
# 2026-05-26 persona-spec rewrite, the LLM strips the verify_deploy_smoke_
# prefix when quoting the marker in its reply. Match the suffix only so the
# polling tolerates both the old and new echo styles.
smoke_marker_suffix="${smoke_marker#verify_deploy_smoke_}"
log "step 4: posting smoke marker $smoke_marker"

smoke_payload=$(cat <<EOF
{
  "type": "new-message",
  "data": {
    "guid": "$smoke_marker",
    "text": "ping $smoke_marker",
    "isFromMe": false,
    "handle": {"address": "$KAVI_SMOKE_HANDLE"},
    "chats": [{"guid": "iMessage;-;$KAVI_SMOKE_HANDLE"}]
  }
}
EOF
)

# Retry the smoke POST up to 5x with 3s backoff. /health can return 200
# within ~3s of `launchctl kickstart -k`, but FastAPI's webhook POST
# handlers (specifically /imessage) aren't reliably ready that quickly —
# the first attempt after a hard restart often fails with connection drop
# or transient 5xx while the new process finishes loading. By the second
# or third attempt the runtime is fully up. Total max retry wait = ~15s.
# Diagnosed 2026-05-12 from a bash -x trace; previously caused spurious
# auto-rollbacks on every fresh deploy.
post_smoke() {
    ssh "$KAVI_HOST" "curl -fsS --max-time 10 -X POST \
        -H 'Content-Type: application/json' \
        --data-binary @- \
        http://127.0.0.1:8080/imessage" <<<"$smoke_payload" >/dev/null
}
post_ok=0
for attempt in 1 2 3 4 5; do
    if post_smoke; then
        post_ok=1
        if (( attempt > 1 )); then
            log "step 4: smoke POST succeeded on attempt $attempt/5"
        fi
        break
    fi
    log "step 4: smoke POST attempt $attempt/5 failed; retrying after 3s"
    sleep 3
done
if (( post_ok == 0 )); then
    fail "smoke webhook POST failed after 5 retries"
fi

# Poll for the chain to complete. Lookup pattern, robust to:
#   - persona voice changes that drop the marker from Kavi's outbound text
#     (broke 2026-05-29 post-persona-collapse: outbound says "Pong, smoke
#     signal received" instead of echoing the marker)
#   - the smoke POST being retried during runtime warmup, where the first
#     attempt's chain may not complete but a later attempt does (each
#     attempt creates a fresh inbound row tied to the same chat)
#
# Strategy: collect ALL inbound_ids whose text contains the smoke marker
# (the retries land as separate rows). Pass if ANY outbound's triggered_by
# matches ANY of those inbound_ids.
deadline=$(( $(date +%s) + SMOKE_TIMEOUT_SECONDS ))
found_inbound_ids=""
found_outbound=0
while (( $(date +%s) < deadline )); do
    inbound_rows=$(ssh "$KAVI_HOST" \
        "curl -fsS --max-time 5 'http://127.0.0.1:8080/evals/kavi-persona/inbound/recent?limit=50'" \
        2>/dev/null || echo "")
    new_ids=$(echo "$inbound_rows" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
marker = '$smoke_marker'
for row in data.get('rows', []):
    if marker in (row.get('text') or ''):
        iid = row.get('inbound_id') or ''
        if iid:
            print(iid)
" 2>/dev/null || echo "")
    if [[ -n "$new_ids" ]]; then
        found_inbound_ids="$new_ids"
    fi
    if [[ -n "$found_inbound_ids" ]]; then
        outbound_rows=$(ssh "$KAVI_HOST" \
            "curl -fsS --max-time 5 'http://127.0.0.1:8080/evals/kavi-persona/recent?limit=50'" \
            2>/dev/null || echo "")
        outbound_hit=$(echo "$outbound_rows" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
targets = set('''$found_inbound_ids'''.split())
for row in data.get('rows', []):
    if (row.get('triggered_by') or '') in targets:
        print('1')
        break
" 2>/dev/null || echo "")
        if [[ "$outbound_hit" == "1" ]]; then
            found_outbound=1
            break
        fi
    fi
    sleep 2
done

if [[ -z "$found_inbound_ids" ]]; then
    fail "smoke marker $smoke_marker did not appear in inbound log within ${SMOKE_TIMEOUT_SECONDS}s"
fi
if (( found_outbound == 0 )); then
    fail "no outbound triggered by any smoke inbound ($found_inbound_ids) within ${SMOKE_TIMEOUT_SECONDS}s"
fi
log "step 4 OK: inbound → outbound chain confirmed"

log "verify_deploy: ALL STEPS PASSED"
