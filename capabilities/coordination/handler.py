"""Kavi coordinates capability — reactive coordination handler.

Implements the four-branch coordination flow per the canonical Rosa cash
few-shot in `capabilities/kavi-coordinates.md`:

  Branch 4a (yes_have_it)    → outcome report only, no task created.
  Branch 4b (will_grab)       → create task in shared list owned by addressee,
                                outcome report.
  Branch 4c (ambiguous)       → clarify with addressee (max 1-2 iterations),
                                escalate to requester if still ambiguous.
  Branch 4d (no_reply)        → judge follow-up window, schedule one re-ping,
                                escalate to requester if still silent.

Session state lives in an in-memory dict keyed by session_id, write-through
persisted to the per-concept state file `coordination_sessions.json`
(sibling of questions.json etc.; see kavi_runtime/state_per_concept.py).
Added 2026-06-10 after the 2026-06-03 deploy restart wiped the in-memory
registry mid-session: Max's reply was misrouted as if from Megha and the
session orphaned. Persistence contract:

  - Every mutation (create, phase transition, close) writes the registry
    through to disk via the canonical atomic-write helper (unique tmp
    filename per writer).
  - The first lookup after a process restart lazy-loads the file back
    into memory; closed sessions and sessions older than the 24h idle
    timeout are pruned at load.
  - Both lookup paths (addressee-side and requester-side) consult the
    persisted state via the same lazy-load.

Cross-day commitments persist via durable_facts (7-day TTL per spec).
Sessions clear on session close OR after 24h idle timeout.

Wired in handlers.py: when the action-intent classifier returns has_action=
false and the coordination-intent classifier returns is_coordination=high,
handlers calls `start_coordination` instead of the conversational composer.
The bypass invariant holds: action-implying inbounds reply via the action
layer, coordination-implying inbounds reply via this handler, neither via
the conversational composer.

Audit: every state transition appends one row to
`eval-coordinates-judgments.jsonl` keyed by session_id.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.runtime import durable_facts
from kavi_runtime.runtime import outbound_scanner
from kavi_runtime.bluebubbles_client import BlueBubblesClient
from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.graph_client import GraphClient
from kavi_runtime.outbound_log import log_outbound
from kavi_runtime.state_per_concept import (
    load_coordination_sessions,
    save_coordination_sessions,
)

logger = logging.getLogger(__name__)


# ---- session state: in-memory registry + write-through persistence ---------

# Keyed by session_id. Each entry holds the full coordination context so
# inbound reply webhooks can look up "which session is this addressee
# replying to" without a per-lookup disk read. Cleared on session close
# or 24h idle timeout (purged by `purge_idle_sessions`). Write-through
# persisted to coordination_sessions.json (2026-06-10) so a runtime
# restart no longer orphans in-flight sessions.
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_SESSION_IDLE_TIMEOUT_SEC = 24 * 60 * 60

# Lazy-load latch: the first lookup/mutation after process start (or after
# `_reset_for_tests`) hydrates `_sessions` from the persisted file. Guarded
# by `_sessions_lock`.
_sessions_loaded = False


def _sessions_legacy_state_path(config: dict | None) -> Path | None:
    """Resolve the legacy imessage-state path the per-concept helpers key
    off (coordination_sessions.json lands as a sibling, same as every other
    per-concept file). Returns None when the config carries no state path —
    persistence then degrades to in-memory-only (unit-test configs)."""
    raw = ((config or {}).get("paths") or {}).get("imessage_state")
    return Path(raw) if raw else None


def _session_age_sec(session: dict[str, Any], now: datetime | None = None) -> float | None:
    """Wall-clock age of a session from its ts_started ISO stamp. Returns
    None when the stamp is missing/unparseable (caller treats as fresh —
    a malformed stamp should not silently drop a live session)."""
    raw = session.get("ts_started")
    if not raw:
        return None
    try:
        started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - started).total_seconds()


def _ensure_sessions_loaded(config: dict | None) -> None:
    """Hydrate the in-memory registry from coordination_sessions.json on the
    first call after process start. Closed sessions and sessions past the
    24h idle timeout are pruned at load (and the pruned view is written
    back so the file does not accumulate stale entries). No-op when the
    config carries no state path or the registry is already hydrated."""
    global _sessions_loaded
    legacy = _sessions_legacy_state_path(config)
    if legacy is None:
        return
    with _sessions_lock:
        if _sessions_loaded:
            return
        try:
            data = load_coordination_sessions(legacy)
        except Exception as e:
            logger.warning(
                "coordination_handler: session-state load failed (continuing "
                "in-memory-only): %s", e,
            )
            _sessions_loaded = True
            return
        persisted = data.get("sessions") or {}
        pruned = 0
        for sid, session in persisted.items():
            if sid in _sessions:
                continue  # in-memory state is newer; keep it.
            if session.get("phase") == "closed":
                pruned += 1
                continue
            age = _session_age_sec(session)
            if age is not None and age > _SESSION_IDLE_TIMEOUT_SEC:
                pruned += 1
                continue
            # Rebase monotonic_started onto THIS process's monotonic clock so
            # latest-wins ordering and idle purge keep working across the
            # restart (the persisted monotonic value belonged to the dead
            # process's clock and is meaningless here).
            session = dict(session)
            session["monotonic_started"] = time.monotonic() - (age or 0.0)
            _sessions[sid] = session
        _sessions_loaded = True
        if persisted:
            logger.info(
                "coordination_handler: restored %d session(s) from disk, "
                "pruned %d closed/stale", len(_sessions), pruned,
            )
    if pruned:
        _persist_sessions(config)


def _persist_sessions(config: dict | None) -> None:
    """Write-through the registry to coordination_sessions.json. Called on
    every session mutation (create, phase transition, close). Failure-safe:
    a persistence error never breaks the live coordination flow — worst
    case is the pre-2026-06-10 behavior (restart loses the session)."""
    legacy = _sessions_legacy_state_path(config)
    if legacy is None:
        return
    try:
        with _sessions_lock:
            snapshot = {sid: dict(s) for sid, s in _sessions.items()}
        save_coordination_sessions(legacy, {"sessions": snapshot})
    except Exception as e:
        logger.warning(
            "coordination_handler: session-state persist failed (non-fatal): %s", e,
        )

# Idempotency: same inbound (matched on a stable hash of inbound_text +
# requester_handle + minute-bucket timestamp) → only one session created.
# Bucket size matches the BlueBubbles webhook re-fire window (~3-5 sec) plus
# slack; 60 sec is safe and stops a re-fire from spawning a duplicate session.
_recent_inbound_hashes: dict[str, str] = {}
_IDEMPOTENCY_TTL_SEC = 90


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_session_id(label: str = "session") -> str:
    """session_id format: s_<utc_iso>_<short_label>. Label is derived from
    the coordination ask (first few words slugified) so the session_id is
    human-readable in audit logs. Falls back to a uuid when no label given."""
    safe_label = "".join(c for c in label.lower() if c.isalnum() or c == "_")[:20] or uuid.uuid4().hex[:8]
    return f"s_{_utc_now_iso()}_{safe_label}"


def _new_decision_id() -> str:
    """One row identifier per phase row in eval-coordinates-judgments.jsonl.
    Format mirrors persona/inbox decision_id: c_<utc_iso>_<8hex>."""
    return f"c_{_utc_now_iso()}_{uuid.uuid4().hex[:8]}"


def _idempotency_key(inbound_text: str, requester_handle: str) -> str:
    """Stable key combining the verbatim inbound text + requester handle +
    minute-bucket timestamp. Catches BlueBubbles webhook re-fires that
    deliver the same payload twice within seconds."""
    bucket = int(time.time() // 60)
    raw = f"{requester_handle}::{inbound_text.strip()}::{bucket}"
    # Plain sha-style hash via Python hash() is process-local; for our purpose
    # a deterministic short string suffices since we only use it for in-memory
    # dedup within the runtime process lifetime.
    import hashlib
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _prune_recent_hashes() -> None:
    """Drop hashes older than IDEMPOTENCY_TTL_SEC. Called opportunistically
    inside the locked path so the dict doesn't grow unbounded."""
    now = time.monotonic()
    stale = [k for k, (_sid, ts) in _recent_inbound_hashes.items() if isinstance(_sid, tuple) and now - ts > _IDEMPOTENCY_TTL_SEC]
    for k in stale:
        del _recent_inbound_hashes[k]


def _resolve_household_name(handle: str | None, config: dict | None = None) -> str:
    """Map an iMessage handle to a household member name. Defaults to
    'unknown' when no match — that case shouldn't reach this code (the
    upstream sender allowlist gate filters non-household handles), but the
    defensive fallback prevents a None showing up in the audit row."""
    if not handle:
        return "unknown"
    h = handle.strip().lower()
    if config:
        megha_phone = (config.get("imessage", {}).get("megha_phone") or "").lower()
        if h == megha_phone:
            return "Megha"
        own_emails = {a.lower() for a in config.get("imessage", {}).get("own_email_addresses", []) if a}
        if h in own_emails:
            return "Megha"
    from kavi_runtime import household
    member = household.member_for_handle(h)
    if member == "megha":
        return "Megha"
    if member == "max":
        return "Max"
    return "unknown"


def _household_members_for_classifier(config: dict | None) -> list[dict[str, str]]:
    """Pass to the coordination-intent classifier. Handles come from the
    private config's household identity (kavi_runtime/household.py)."""
    from kavi_runtime import household
    return [
        {"name": "Megha", "handle": household.primary_phone("megha")},
        {"name": "Max", "handle": household.primary_phone("max")},
    ]


# ---- audit log -------------------------------------------------------------


def _eval_coordinates_path(config: dict) -> Path:
    """Path to eval-coordinates-judgments.jsonl. Single source of truth for
    the audit trail of coordination state transitions. Read by
    /eval-coordinates via HTTP from Megha's Mac."""
    raw = config.get("paths", {}).get("eval_coordinates_judgments_jsonl")
    if raw:
        path = Path(raw)
    else:
        # Defensive default — should not be hit because config.yaml carries
        # the path explicitly. Keeps the runtime from crashing on a config
        # regression that drops the key.
        path = Path("/Users/kavi/HomeOS/evals/kavi-coordinates/eval-coordinates-judgments.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _log_phase(
    config: dict,
    *,
    session: dict[str, Any],
    phase: str,
    extras: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    closed: bool = False,
) -> None:
    """Append one phase row to eval-coordinates-judgments.jsonl. Schema per
    `evals/definitions.md`. Failure-safe: any IO error is logged and
    swallowed."""
    try:
        record: dict[str, Any] = {
            "decision_id": _new_decision_id(),
            "session_id": session["session_id"],
            "ts": _utc_now_iso(),
            "capability": "kavi-coordinates",
            "phase": phase,
            "requester_handle": session.get("requester_handle"),
            "addressee_handle": session.get("addressee_handle"),
            "inbound_text": session.get("inbound_text") if phase == "ack" else None,
            "ack_text": session.get("ack_text") if phase == "ack" else None,
            "ack_latency_sec": session.get("ack_latency_sec") if phase == "ack" else None,
            "addressee_message_text": session.get("addressee_message_text") if phase == "addressee_reach" else None,
            "addressee_message_attribution": session.get("addressee_message_attribution") if phase == "addressee_reach" else None,
            "addressee_reply_text": session.get("addressee_reply_text") if phase == "addressee_reply" else None,
            "addressee_reply_latency_sec": session.get("addressee_reply_latency_sec") if phase == "addressee_reply" else None,
            "branch": session.get("branch") if phase == "branch_decision" else None,
            "task_created": session.get("task_created", False),
            "task_id": session.get("task_id"),
            "outcome_report_text": session.get("outcome_report_text") if phase == "outcome_report" else None,
            "outcome_report_latency_sec": session.get("outcome_report_latency_sec") if phase == "outcome_report" else None,
            "closed": closed,
            "usage": usage,
        }
        if extras:
            record["extras"] = extras
        with _eval_coordinates_path(config).open("a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning("coordination_handler._log_phase failed (non-fatal): %s", e)


# ---- send helper -----------------------------------------------------------


def _send_with_scanner(
    config: dict,
    *,
    bb: BlueBubblesClient,
    text: str,
    recipient_handle: str,
    kind: str,
    chat_guid_override: str | None = None,
    provenance: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Coordination-side send wrapper. Routes through the canonical
    `handlers._send_imessage_with_fallback` so every persona-voiced outbound
    in the runtime exits a single function (recipient allowlist + content
    scanner + verify-after-send + Outlook fallback + eval log). The wrapper
    keeps its prior return shape so existing coordination tests pass without
    rewiring.

    Coordination messages to non-Megha addressees (e.g., Max): the canonical
    sender resolves the chat_guid for the addressee. BlueBubbles convention:
    address-based DM threads use `iMessage;-;<handle>` for non-Megha
    addressees; the legacy `any;-;<handle>` form remains for Megha's chat.
    The `chat_guid_override` parameter is preserved for tests but no longer
    forks send logic.

    Returns: {"sent", "verified", "blocked", "blocked_reason", "temp_guid"}
    """
    # Build a coordination-tagged kind so the eval surface keeps the
    # `coordination_<kind>` namespace prior callers expect.
    coord_kind = f"coordination_{kind}"

    # Delayed import to break the import cycle (handlers imports
    # coordination_handler at module load via `lookup_session_by_addressee_handle`).
    from kavi_runtime import handlers as _handlers

    send_result = _handlers._send_imessage_with_fallback(
        config, text, kind=coord_kind, recipient_handle=recipient_handle,
        provenance=provenance,
    )

    # Coordination flow expects `blocked` + `blocked_reason` in the dict
    # shape. The canonical wrapper already populates both fields.
    return {
        "sent": send_result.get("sent", False),
        "verified": send_result.get("verified", False),
        "blocked": send_result.get("blocked", False),
        "blocked_reason": send_result.get("blocked_reason"),
        "temp_guid": send_result.get("temp_guid"),
    }


# ---- public api: the four-branch flow --------------------------------------


def start_coordination(
    *,
    inbound_text: str,
    requester_handle: str,
    addressee_handle: str,
    addressee_name: str,
    coordination_ask: str,
    config: dict,
    claude: ClaudeClient,
    graph: GraphClient,
    bb: BlueBubblesClient,
    inbound_received_ts: float | None = None,
) -> dict[str, Any]:
    """Begin a coordination session.

    Steps (per spec "Expected action sequence"):
      1. Ack the requester via BlueBubbles SEND.
      2. Record durable fact capturing the coordination intent.
      3. Compose + send the addressee message.
      4. Wait for reply (handler stays alive via lifecycle module; the
         BlueBubbles webhook routes the reply back through the imessage
         webhook handler in handlers.py, which calls
         `handle_addressee_reply` when it sees a reply from the addressee).

    Returns a status dict for the caller to log + return upstream.
    """
    if inbound_received_ts is None:
        inbound_received_ts = time.time()

    # Hydrate persisted sessions first so the idempotency check and any
    # latest-wins ordering see pre-restart sessions too.
    _ensure_sessions_loaded(config)

    # ---- idempotency check ------------------------------------------------
    idem_key = _idempotency_key(inbound_text, requester_handle)
    with _sessions_lock:
        existing_sid = _recent_inbound_hashes.get(idem_key)
        if existing_sid:
            logger.info(
                "start_coordination: idempotency hit on key=%s existing session=%s",
                idem_key, existing_sid,
            )
            return {"status": "coordination_idempotency_hit", "session_id": existing_sid}

    # ---- new session ------------------------------------------------------
    requester_name = _resolve_household_name(requester_handle, config)
    label_words = (coordination_ask or inbound_text or "")[:30].split()
    label = "_".join(label_words[:3]) if label_words else "session"
    session_id = _new_session_id(label)

    session: dict[str, Any] = {
        "session_id": session_id,
        "ts_started": _utc_now_iso(),
        "monotonic_started": time.monotonic(),
        "inbound_received_ts": inbound_received_ts,
        "inbound_text": inbound_text,
        "requester_handle": requester_handle,
        "requester_name": requester_name,
        "addressee_handle": addressee_handle,
        "addressee_name": addressee_name,
        "coordination_ask": coordination_ask,
        "phase": "starting",
        "task_created": False,
        "task_id": None,
        "follow_up_count": 0,
        "clarification_count": 0,
        # Requester-facing follow-up windows (2026-06-22), inferred from the
        # ask's urgency. soft = when to tell the requester "still waiting";
        # hard = when to say "they haven't responded — follow up or leave it?".
        # The sweep (run_coordination_followup_sweep) fires each at most once.
        **dict(zip(
            ("soft_window_sec", "hard_window_sec"),
            _infer_followup_window(coordination_ask or inbound_text),
        )),
        "soft_followup_sent": False,
        "hard_followup_sent": False,
    }

    with _sessions_lock:
        _sessions[session_id] = session
        _recent_inbound_hashes[idem_key] = session_id
    _persist_sessions(config)

    # ---- Step 2 (ack the requester) — done first so the requester gets
    # immediate acknowledgement per acceptance criterion #1 (within 10s).
    _send_ack(session, config, claude, bb)

    # ---- Step 3 (durable-facts record of the coordination intent) --------
    _record_intent_fact(session, config)

    # ---- Step 4 (message the addressee) ----------------------------------
    _send_addressee_message(session, config, claude, bb)

    return {
        "status": "coordination_started",
        "session_id": session_id,
        "addressee_name": addressee_name,
    }


def _send_ack(
    session: dict[str, Any],
    config: dict,
    claude: ClaudeClient,
    bb: BlueBubblesClient,
) -> None:
    """Compose + send the requester ack within 10 sec of inbound landing."""
    ack_started = time.monotonic()
    composed = claude.compose_coordination_ack(
        inbound_text=session["inbound_text"],
        addressee_name=session["addressee_name"],
    )
    # AUDIT 2026-06-10 (fallback branch): honest "checking with X" sentence,
    # no completed-action claim.
    ack_text = composed.get("text") or f"Got it, checking with {session['addressee_name']} now. I'll let you know what they say."
    ack_provenance = (
        {"llm_call": "compose_coordination_ack"} if composed.get("text")
        else {"fallback_audit": "2026-06-10"}
    )

    send_result = _send_with_scanner(
        config, bb=bb, text=ack_text,
        recipient_handle=session["requester_handle"],
        kind="ack",
        provenance=ack_provenance,
    )
    session["ack_text"] = ack_text
    session["ack_latency_sec"] = int(time.time() - session["inbound_received_ts"])
    session["ack_send_result"] = send_result
    _persist_sessions(config)

    _log_phase(config, session=session, phase="ack", usage=composed.get("_usage"))


def _record_intent_fact(session: dict[str, Any], config: dict) -> None:
    """Record the coordination intent in durable_facts. 7-day TTL per spec.
    Pre-checks the durable-facts write filter so a fact_text containing a
    sensitive pattern is rejected before the storage layer runs."""
    fact_text = (
        f"{session['requester_name']} asked Kavi to coordinate with "
        f"{session['addressee_name']} on: {session['coordination_ask']}"
    )
    allowed, reason = outbound_scanner.gate_durable_fact_write(
        config=config, fact_text=fact_text,
    )
    if not allowed:
        logger.warning(
            "coordination_handler: durable-fact write blocked session=%s reason=%s",
            session["session_id"], reason,
        )
        return
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # Inbound-sourced (the requester's iMessage text composed
        # session['coordination_ask']). Per Fix 3 of 2026-05-06 audit, this
        # fact goes to pending_facts.jsonl and waits for Megha confirmation
        # rather than persisting on this turn.
        durable_facts.record_fact(
            fact_text=fact_text,
            scope="household",
            source_decision_id=session["session_id"],
            expires_at=expires_at,
            config=config,
            originated_from_inbound_content=True,
            inbound_source=f"imessage coord-{session['session_id']}",
        )
    except Exception as e:
        logger.warning("coordination_handler: record_fact failed (non-fatal): %s", e)


def _send_addressee_message(
    session: dict[str, Any],
    config: dict,
    claude: ClaudeClient,
    bb: BlueBubblesClient,
) -> None:
    """Compose + send the message to the addressee. Attribution judgment
    runs inside the composer prompt per principle 1; the runtime passes a
    default suggestion based on inbound shape but the prompt may flip it."""
    # Default attribution suggestion: attribute when the inbound is a personal
    # ask (contains "we" / "I" / "my"), omit for generic household tasks. The
    # composer prompt has the final say.
    inbound_lower = (session["inbound_text"] or "").lower()
    default_attribute = any(p in inbound_lower for p in [" we ", " i ", " my ", " our "])
    attribution_judgment = {
        "should_attribute": default_attribute,
        "reason": "personal-ask heuristic" if default_attribute else "no personal pronouns; treating as household",
    }

    composed = claude.compose_coordination_addressee_message(
        inbound_text=session["inbound_text"],
        requester_name=session["requester_name"],
        addressee_name=session["addressee_name"],
        coordination_ask=session["coordination_ask"],
        attribution_judgment=attribution_judgment,
    )
    addressee_text = composed.get("text")
    addressee_provenance = {"llm_call": "compose_coordination_addressee_message"}
    if not addressee_text:
        # Persona refused or composer errored. Honest fallback: relay a
        # minimal templated ask so the coordination doesn't silently die.
        # AUDIT 2026-06-10: relays the verbatim ask, no action claims.
        addressee_text = f"Hey {session['addressee_name']}, {session['coordination_ask']}"
        addressee_provenance = {"fallback_audit": "2026-06-10"}
    attribution_applied = bool(composed.get("attribution_applied", False))

    send_result = _send_with_scanner(
        config, bb=bb, text=addressee_text,
        recipient_handle=session["addressee_handle"],
        kind="addressee_reach",
        provenance=addressee_provenance,
    )

    session["addressee_message_text"] = addressee_text
    session["addressee_message_attribution"] = attribution_applied
    session["addressee_reach_send_result"] = send_result
    session["phase"] = "awaiting_addressee_reply"
    session["addressee_reach_ts"] = time.time()
    _persist_sessions(config)

    _log_phase(config, session=session, phase="addressee_reach", usage=composed.get("_usage"))


def lookup_session_by_addressee_handle(
    addressee_handle: str, config: dict | None = None,
) -> dict[str, Any] | None:
    """Find an active session waiting on a reply from the given handle.
    Returns the latest matching session (most-recently-started). None when
    no session is awaiting a reply from that addressee.

    Why latest-wins: a reply to an older session would have come in before
    a newer one started; the most-recent session is the most likely target.
    Multi-session-per-addressee is uncommon and the runtime today doesn't
    support it; this lookup is the single decision point for routing replies.

    `config` (added 2026-06-10): when provided, the lookup consults the
    persisted session file on the first call after a restart. Pass it from
    every production call site — the 2026-06-03 incident was exactly this
    lookup returning None on a fresh (empty) in-memory dict after a deploy
    restart, which misrouted Max's reply into the persona layers.
    """
    _ensure_sessions_loaded(config)
    with _sessions_lock:
        candidates = [
            s for s in _sessions.values()
            if s.get("phase") in {"awaiting_addressee_reply", "awaiting_clarification_reply"}
            and s.get("addressee_handle") == addressee_handle
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.get("monotonic_started", 0))


def lookup_session_by_requester_handle(
    requester_handle: str, config: dict | None = None,
) -> dict[str, Any] | None:
    """Find an active coordination session whose REQUESTER is the given handle
    and whose addressee_reach has fired (so the session is awaiting an
    addressee reply or clarification). Returns the latest matching session.

    Added 2026-05-07 for Bug 2: when Megha sends a follow-up mid-coordination
    ("you sent it to me, not Max"), the inbound handler needs to consider
    routing it to the coordination layer for a possible retry — not directly
    to the conversational composer, which previously fabricated an unverified
    "resending now" reply.

    `config` (added 2026-06-10): same persisted-state hydration contract as
    `lookup_session_by_addressee_handle`.

    None when no active session has this handle as the requester.
    """
    _ensure_sessions_loaded(config)
    with _sessions_lock:
        candidates = [
            s for s in _sessions.values()
            if s.get("phase") in {"awaiting_addressee_reply", "awaiting_clarification_reply"}
            and s.get("requester_handle") == requester_handle
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.get("monotonic_started", 0))


def list_open_sessions(config: dict | None = None) -> list[dict[str, Any]]:
    """Compact view of every open (non-closed) coordination session for the
    intent parser's context (2026-06-10 intent-first dispatch rebuild).
    Returns [{session_id, addressee, ask}], oldest first. Failure-safe."""
    _ensure_sessions_loaded(config)
    with _sessions_lock:
        sessions = [
            s for s in _sessions.values() if s.get("phase") != "closed"
        ]
    sessions.sort(key=lambda s: s.get("monotonic_started", 0))
    return [
        {
            "session_id": s.get("session_id"),
            "addressee": s.get("addressee_name", ""),
            "ask": s.get("coordination_ask", ""),
        }
        for s in sessions
    ]


def handle_requester_course_correction(
    *,
    session_id: str,
    follow_up_text: str,
    config: dict,
    claude: ClaudeClient,
    bb: BlueBubblesClient,
) -> dict[str, Any] | None:
    """Process a mid-coordination follow-up from the requester. Classifies via
    `classify_coordination_course_correction`; on a high-confidence
    course-correction, re-fires `addressee_reach` and replies to the requester
    with a tool-grounded confirmation (verified-from-tool-result language —
    NEVER an LLM-composed "resending" string without a real send).

    Returns:
      - dict with status="coordination_retry_addressee_reach_<verified|failed>"
        on a course-correction match.
      - None when the follow-up is NOT a course-correction; the caller falls
        through to the normal conversational / action / coordination-intent
        layers.

    The retry uses the SAME composed addressee message from the original
    addressee_reach. We don't recompose: the Bug 2 root cause was wire
    routing, not message content. Recomposing would double the cost and
    introduce a window where the second send drifts from the first.
    """
    _ensure_sessions_loaded(config)
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        logger.info(
            "handle_requester_course_correction: session_id=%s not found", session_id,
        )
        return None

    # Classify whether this follow-up is a course-correction. Conservative —
    # only act on high confidence. Anything else falls through.
    classify_result = claude.classify_coordination_course_correction(
        reply_text=follow_up_text,
        prior_addressee_message=session.get("addressee_message_text", ""),
        addressee_name=session["addressee_name"],
        coordination_ask=session.get("coordination_ask", ""),
    )
    is_cc = classify_result.get("is_course_correction", False)
    confidence = classify_result.get("confidence", "low")
    if not is_cc or confidence != "high":
        return None

    # Re-fire addressee_reach with the original composed text. If we never
    # captured a composed text (defensive — shouldn't happen for an
    # awaiting_addressee_reply session, since _send_addressee_message sets it
    # before flipping phase), fall through.
    addressee_text = session.get("addressee_message_text") or ""
    if not addressee_text:
        logger.warning(
            "handle_requester_course_correction: session=%s missing "
            "addressee_message_text; falling through",
            session_id,
        )
        return None

    # Bump retry counter for the audit trail. Cap at 1 retry per session so a
    # mis-classified course-correction can't spiral into a loop.
    retries = session.get("course_correction_retries", 0)
    if retries >= 1:
        logger.info(
            "handle_requester_course_correction: session=%s already retried; "
            "not re-firing", session_id,
        )
        return None

    # Re-send of the previously LLM-composed addressee message: provenance
    # is the composer call that produced the stored text.
    send_result = _send_with_scanner(
        config, bb=bb, text=addressee_text,
        recipient_handle=session["addressee_handle"],
        kind="addressee_reach_retry",
        provenance={"llm_call": "compose_coordination_addressee_message"},
    )
    session["course_correction_retries"] = retries + 1
    session["addressee_reach_ts"] = time.time()
    session["last_retry_send_result"] = send_result
    _persist_sessions(config)
    _log_phase(
        config, session=session, phase="addressee_reach_retry",
        usage=classify_result.get("_usage"),
        extras={
            "course_correction_reason": classify_result.get("reason"),
            "course_correction_confidence": classify_result.get("confidence"),
            "follow_up_text": follow_up_text[:240],
            "retry_count": session["course_correction_retries"],
            "send_verified": send_result.get("verified", False),
            "send_blocked": send_result.get("blocked", False),
            "blocked_reason": send_result.get("blocked_reason"),
        },
    )

    # Compose the requester ack from VERIFIED tool-result fields only. Per
    # the role contract: no LLM-composed "done" claims here. The text is
    # entirely deterministic on the boolean + reason.
    addressee_name = session["addressee_name"]
    if send_result.get("blocked"):
        ack_text = (
            f"Tried to retry to {addressee_name}, but the send was blocked: "
            f"{send_result.get('blocked_reason') or 'unknown reason'}."
        )
        retry_status = "coordination_retry_addressee_reach_blocked"
    elif send_result.get("verified"):
        ack_text = (
            f"Retried — {addressee_name}'s chat shows the message landed. "
            "I'll let you know when they reply."
        )
        retry_status = "coordination_retry_addressee_reach_verified"
    elif send_result.get("sent"):
        # sent but not yet verified (BlueBubbles sometimes lags on the verify
        # poll); be honest about the partial state rather than overclaim.
        ack_text = (
            f"Retried to {addressee_name}. Send went through but I haven't "
            "confirmed delivery yet — I'll surface a fallback if it doesn't "
            "land."
        )
        retry_status = "coordination_retry_addressee_reach_unverified"
    else:
        ack_text = (
            f"Retry to {addressee_name} failed at the send layer. I'll try the "
            "Outlook fallback next."
        )
        retry_status = "coordination_retry_addressee_reach_failed"

    # Conformance-sweep finding (2026-06-10): deterministic retry-status
    # templates. AUDIT 2026-06-10: each branch's claim is grounded in the
    # retry send_result computed directly above. Listed for LLM migration.
    _send_with_scanner(
        config, bb=bb, text=ack_text,
        recipient_handle=session["requester_handle"],
        kind="course_correction_ack",
        provenance={"fallback_audit": "2026-06-10"},
    )

    return {
        "status": retry_status,
        "session_id": session_id,
        "addressee_name": addressee_name,
        "send_verified": send_result.get("verified", False),
        "send_blocked": send_result.get("blocked", False),
    }


def handle_addressee_reply(
    *,
    session_id: str,
    reply_text: str,
    config: dict,
    claude: ClaudeClient,
    graph: GraphClient,
    bb: BlueBubblesClient,
) -> dict[str, Any]:
    """Process the addressee's reply. Parses via LLM into the branch
    decision and dispatches to the matching branch handler.
    """
    _ensure_sessions_loaded(config)
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        logger.warning("handle_addressee_reply: session_id=%s not found (timeout?)", session_id)
        return {"status": "session_not_found", "session_id": session_id}

    session["addressee_reply_text"] = reply_text
    session["addressee_reply_latency_sec"] = (
        int(time.time() - session["addressee_reach_ts"])
        if session.get("addressee_reach_ts") else None
    )
    _persist_sessions(config)
    _log_phase(config, session=session, phase="addressee_reply")

    parsed = claude.parse_coordination_reply(
        reply_text=reply_text,
        prior_addressee_message=session.get("addressee_message_text", ""),
        addressee_name=session["addressee_name"],
        coordination_ask=session["coordination_ask"],
    )
    branch = parsed.get("branch", "4c_ambiguous")
    confidence = parsed.get("confidence", "low")

    # Conservative: low confidence on a 4b_will_grab → demote to 4c so the
    # handler clarifies with the addressee instead of creating a task on a
    # fabricated commitment. This mirrors the action layer's match-confidence
    # gate; the spec calls it out as the "reply-parse confidence below
    # threshold" guardrail.
    if branch == "4b_will_grab" and confidence == "low":
        logger.info(
            "handle_addressee_reply: 4b_will_grab demoted to 4c_ambiguous on low confidence session=%s",
            session_id,
        )
        branch = "4c_ambiguous"

    session["branch"] = branch
    session["parsed_reply"] = parsed
    _persist_sessions(config)
    _log_phase(config, session=session, phase="branch_decision", usage=parsed.get("_usage"))

    # Record the addressee's commitment as a durable fact (7-day TTL) so a
    # later coordination can reference it.
    if branch in {"4a_yes_have_it", "4b_will_grab"}:
        commitment = parsed.get("commitment_text") or ""
        if commitment:
            scope = "max" if session["addressee_name"].lower() == "max" else "megha"
            allowed, reason = outbound_scanner.gate_durable_fact_write(
                config=config, fact_text=commitment,
            )
            if allowed:
                expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    # Inbound-sourced (parsed from the addressee's iMessage
                    # reply). Per Fix 3 of 2026-05-06 audit, queues for
                    # confirmation rather than persisting on this turn.
                    durable_facts.record_fact(
                        fact_text=commitment, scope=scope,
                        source_decision_id=session_id,
                        expires_at=expires_at, config=config,
                        originated_from_inbound_content=True,
                        inbound_source=f"imessage coord-{session_id} addressee_reply",
                    )
                except Exception as e:
                    logger.warning("coordination_handler: record_fact (commitment) failed: %s", e)

    # Dispatch by branch.
    if branch == "4a_yes_have_it":
        return _branch_4a_no_task(session, config, claude, bb)
    if branch == "4b_will_grab":
        return _branch_4b_create_task(session, config, claude, graph, bb)
    if branch == "4c_ambiguous":
        return _branch_4c_clarify_addressee(session, config, claude, bb)
    # Defensive: unknown branch falls to ambiguous handling.
    logger.warning("handle_addressee_reply: unexpected branch=%r; treating as 4c", branch)
    return _branch_4c_clarify_addressee(session, config, claude, bb)


def _branch_4a_no_task(
    session: dict[str, Any],
    config: dict,
    claude: ClaudeClient,
    bb: BlueBubblesClient,
) -> dict[str, Any]:
    """Branch 4a: addressee already has it. Send outcome report; close
    session. NO task created."""
    session["task_created"] = False
    return _send_outcome_report(session, config, claude, bb, branch="4a_yes_have_it")


def _branch_4b_create_task(
    session: dict[str, Any],
    config: dict,
    claude: ClaudeClient,
    graph: GraphClient,
    bb: BlueBubblesClient,
) -> dict[str, Any]:
    """Branch 4b: addressee committed. Create a task in the McMullen-Jain
    Shared list owned by the addressee, then send outcome report.
    """
    parsed = session.get("parsed_reply", {})
    title = parsed.get("task_title_proposal") or session.get("coordination_ask") or "Coordination task"
    deadline_raw = parsed.get("deadline")
    # Append the natural-language deadline phrase to the title so the
    # downstream surface still surfaces it (spec: branch 4b creates
    # `MM Withdraw cash for Rosa (by Mon 2pm)`).
    if deadline_raw and deadline_raw not in title.lower():
        title = f"{title} (by {deadline_raw})"
    title = title[:200]

    owner_prefix = "MM" if session["addressee_name"].lower() == "max" else "MJ"
    list_id = config["graph"]["mstodo_shared_list_id"]
    source_imessage_id = f"coord-{session['session_id']}"

    task_id: str | None = None
    created = False
    create_task_failure_reason: str | None = None
    try:
        task_id, created = graph.create_task_in_shared_list(
            list_id, title=title, owner_prefix=owner_prefix,
            deadline=None, source_imessage_id=source_imessage_id,
        )
        logger.info(
            "coordination 4b create: session=%s task_id=%s created=%s title=%r",
            session["session_id"], (task_id or "")[:12], created, title[:80],
        )
    except Exception as e:
        logger.exception("coordination 4b create_task failed: %s", e)
        create_task_failure_reason = str(e)[:200]

    session["task_created"] = bool(task_id)
    session["task_id"] = task_id
    session["task_title_rendered"] = f"{owner_prefix} {title}" if task_id else None

    # F3 (2026-05-07): when create_task fails, report it honestly to the
    # requester instead of silently shipping the no-task outcome message.
    # Pre-fix behavior: branch stayed `4b_will_grab`; the composer was told
    # NOT to mention task creation; "Max said he'll grab it" went out with
    # no signal that the tracking task was missing. Now the branch flips
    # so the composer can produce a heads-up reply.
    if create_task_failure_reason is not None:
        session["task_create_failure_reason"] = create_task_failure_reason
        return _send_outcome_report(
            session, config, claude, bb, branch="4b_will_grab_task_create_failed",
        )

    return _send_outcome_report(session, config, claude, bb, branch="4b_will_grab")


def _branch_4c_clarify_addressee(
    session: dict[str, Any],
    config: dict,
    claude: ClaudeClient,
    bb: BlueBubblesClient,
) -> dict[str, Any]:
    """Branch 4c: addressee reply is ambiguous. Compose a clarifying
    message back to the addressee. Max 2 clarification iterations before
    escalating to the requester (per spec: "1-2 follow-ups").
    """
    session["clarification_count"] = session.get("clarification_count", 0) + 1
    if session["clarification_count"] > 2:
        # Escalate to requester per principle 3 last-resort.
        session["branch"] = "4c_ambiguous_escalated"
        return _send_outcome_report(session, config, claude, bb, branch="4c_ambiguous_escalated")

    # Compose a clarifying message to the addressee. The composer prompt is
    # the addressee-message composer reused with the ambiguous reply quoted
    # back. We construct the prompt inline rather than adding a 5th method
    # because this is a single bounded use case.
    parsed = session.get("parsed_reply", {})
    quoted_reply = (parsed.get("commitment_text") or session.get("addressee_reply_text") or "")[:80]
    clarify_text = (
        f"Want to make sure I read that right — interpreting '{quoted_reply}' as a yes, "
        "a no, or something else?"
    )[:240]

    # Conformance-sweep finding (2026-06-10): deterministic clarify
    # question. AUDIT 2026-06-10: honest question quoting the ambiguous
    # reply, no action claims. Listed for LLM migration.
    send_result = _send_with_scanner(
        config, bb=bb, text=clarify_text,
        recipient_handle=session["addressee_handle"],
        kind="clarify",
        provenance={"fallback_audit": "2026-06-10"},
    )
    session["phase"] = "awaiting_clarification_reply"
    session["last_clarify_text"] = clarify_text
    _persist_sessions(config)

    _log_phase(config, session=session, phase="follow_up_sent", extras={
        "clarification_count": session["clarification_count"],
    })
    return {
        "status": "coordination_clarifying_addressee",
        "session_id": session["session_id"],
        "clarification_count": session["clarification_count"],
    }


# ---- requester-facing follow-up sweep (2026-06-22) --------------------------

_HOUR = 3600


def _infer_followup_window(ask: str | None) -> tuple[int, int]:
    """Infer (soft_window_sec, hard_window_sec) from the ask's urgency — how
    long to wait before telling the requester 'still waiting' (soft) and
    'they haven't responded, follow up or leave it?' (hard). Deterministic
    keyword heuristic; the hard window is always capped at 24h so a silent
    addressee never strands the requester. This is upstream CONTEXT (a
    timing signal), not user-facing text, so it stays deterministic."""
    a = (ask or "").lower()
    urgent = ("now", "asap", "right away", "urgent", "today", "tonight",
              "this morning", "this afternoon", "by end of day", "eod")
    if any(k in a for k in urgent):
        return (2 * _HOUR, 6 * _HOUR)
    if "tomorrow" in a:
        return (6 * _HOUR, 20 * _HOUR)
    return (4 * _HOUR, 24 * _HOUR)


def _send_still_waiting(
    session: dict[str, Any], config: dict,
    claude: ClaudeClient, bb: BlueBubblesClient,
) -> None:
    """Soft checkpoint: tell the REQUESTER we're still waiting on the
    addressee — status only, no re-narration of the addressee's to-dos
    (principle 2). Does NOT close the session."""
    composed = {}
    try:
        composed = claude.compose_coordination_outcome(
            branch="4d_still_waiting",
            commitment_text=None,
            task_id_if_created=None,
            task_title_if_created=None,
            requester_name=session["requester_name"],
            addressee_name=session["addressee_name"],
            task_create_failure_reason=None,
        ) or {}
    except Exception as e:
        logger.warning("still_waiting: outcome compose failed (using fallback): %s", e)
    text = composed.get("text")
    provenance = {"llm_call": "compose_coordination_outcome"}
    if not text:
        # Safe sentence (cold-fallback policy): no action claims, no
        # re-narration of the addressee's pending to-dos.
        text = (
            f"Still waiting to hear back from {session['addressee_name']} — "
            "I'll let you know the moment they reply."
        )
        provenance = {"fallback_audit": "2026-06-22"}
    _send_with_scanner(
        config, bb=bb, text=text,
        recipient_handle=session["requester_handle"],
        kind="follow_up_status",
        provenance=provenance,
    )
    _log_phase(config, session=session, phase="still_waiting_sent", extras={
        "soft_window_sec": session.get("soft_window_sec"),
    })


def run_coordination_followup_sweep(config: dict) -> dict[str, Any]:
    """Scheduler sweep (interval job): for every session still awaiting the
    addressee, fire the two requester-facing checkpoints when their inferred
    windows elapse — soft 'still waiting' (once), then hard 'they haven't
    responded, follow up or leave it?' (once, which closes the session). Each
    checkpoint fires at most once per session (guarded by the *_followup_sent
    flags) so the sweep can run every few minutes without nagging. Skipped
    during quiet hours so neither checkpoint lands overnight."""
    from kavi_runtime.runtime.clients import _get_clients
    from kavi_runtime.state import is_quiet_hours

    _ensure_sessions_loaded(config)
    if is_quiet_hours(config):
        return {"status": "quiet_hours_skip", "fired": 0}

    with _sessions_lock:
        awaiting = [
            dict(s) | {"session_id": sid}
            for sid, s in _sessions.items()
            if s.get("phase") in {"awaiting_addressee_reply",
                                  "awaiting_clarification_reply"}
        ]
    if not awaiting:
        return {"status": "no_awaiting_sessions", "fired": 0}

    _, claude, bb = _get_clients(config)
    fired = 0
    for snap in awaiting:
        sid = snap["session_id"]
        with _sessions_lock:
            session = _sessions.get(sid)
        if not session or session.get("phase") not in {
            "awaiting_addressee_reply", "awaiting_clarification_reply",
        }:
            continue
        age = _session_age_sec(session) or 0.0
        hard_due = session.get("hard_window_sec", 24 * _HOUR)
        soft_due = session.get("soft_window_sec", 4 * _HOUR)

        if age >= hard_due and not session.get("hard_followup_sent"):
            session["hard_followup_sent"] = True
            _persist_sessions(config)
            # Reuses the existing escalation: requester gets "Haven't heard
            # back from <addressee>. Want me to follow up or take it from
            # here?" and the session closes.
            session["branch"] = "4d_no_reply_escalated"
            _send_outcome_report(
                session, config, claude, bb, branch="4d_no_reply_escalated",
            )
            fired += 1
            continue

        if age >= soft_due and not session.get("soft_followup_sent"):
            session["soft_followup_sent"] = True
            _persist_sessions(config)
            _send_still_waiting(session, config, claude, bb)
            fired += 1

    return {"status": "swept", "awaiting": len(awaiting), "fired": fired}


def _branch_4d_judge_follow_up(
    session_id: str,
    config: dict,
    claude: ClaudeClient,
    bb: BlueBubblesClient,
) -> dict[str, Any]:
    """Branch 4d: the addressee hasn't replied within the judged window.
    Send one re-ping. If still silent after a second judged window, escalate
    to the requester.

    Today the judged window is encoded as a fixed 30-min default — the
    persona-based judgment ("urgency × time of day") is deferred to v0.1
    of this capability. The hook is the right shape; the policy is hard-
    coded for v0 to ship the loop. When the persona-based judgment lands,
    only this function's body changes.
    """
    _ensure_sessions_loaded(config)
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        return {"status": "session_not_found", "session_id": session_id}
    if session.get("phase") != "awaiting_addressee_reply":
        # Reply already arrived, or session moved past this state. No-op.
        return {"status": "no_op_session_advanced", "session_id": session_id}

    session["follow_up_count"] = session.get("follow_up_count", 0) + 1
    # Spec guardrail: max 3 re-pings per session. Beyond that, escalate.
    if session["follow_up_count"] > 3:
        session["branch"] = "4d_no_reply_escalated"
        return _send_outcome_report(session, config, claude, bb, branch="4d_no_reply_escalated")

    if session["follow_up_count"] >= 2:
        # Second re-ping → escalate to requester.
        session["branch"] = "4d_no_reply_escalated"
        return _send_outcome_report(session, config, claude, bb, branch="4d_no_reply_escalated")

    # Conformance-sweep finding (2026-06-10): deterministic re-ping.
    # AUDIT 2026-06-10: honest bump, no action claims. Listed for LLM
    # migration.
    re_ping_text = f"Hey {session['addressee_name']}, just bumping this — any update?"
    _send_with_scanner(
        config, bb=bb, text=re_ping_text,
        recipient_handle=session["addressee_handle"],
        kind="follow_up",
        provenance={"fallback_audit": "2026-06-10"},
    )
    _persist_sessions(config)
    _log_phase(config, session=session, phase="follow_up_sent", extras={
        "follow_up_count": session["follow_up_count"],
    })
    return {
        "status": "coordination_follow_up_sent",
        "session_id": session_id,
        "follow_up_count": session["follow_up_count"],
    }


def _send_outcome_report(
    session: dict[str, Any],
    config: dict,
    claude: ClaudeClient,
    bb: BlueBubblesClient,
    branch: str,
) -> dict[str, Any]:
    """Compose + send the outcome report to the requester. Per principle 2:
    outcome-only, no internal-mechanic narration. Closes the session."""
    parsed = session.get("parsed_reply", {})
    composed = claude.compose_coordination_outcome(
        branch=branch,
        commitment_text=parsed.get("commitment_text") if parsed else None,
        task_id_if_created=session.get("task_id"),
        task_title_if_created=session.get("task_title_rendered"),
        requester_name=session["requester_name"],
        addressee_name=session["addressee_name"],
        task_create_failure_reason=session.get("task_create_failure_reason"),
    )
    outcome_text = composed.get("text")
    if not outcome_text:
        # Honest cold fallback per branch.
        # AUDIT 2026-05-29 (Phase 1 cold-fallback audit): per-branch past-
        # tense, ≤120 chars per branch, prose, first-person, no enumeration,
        # action-grounded (only fires after real coordination outcome with
        # real session state). Kept in place. Re-audit when persona spec
        # changes or new coordination branches are added.
        if branch == "4a_yes_have_it":
            outcome_text = f"{session['addressee_name']} said they have it."
        elif branch == "4b_will_grab":
            outcome_text = f"{session['addressee_name']} said they'll grab it."
        elif branch == "4b_will_grab_task_create_failed":
            outcome_text = (
                f"{session['addressee_name']} said they'll grab it. "
                "Heads up, I couldn't add a task to track it. Want me to retry?"
            )
        elif branch == "4c_ambiguous_escalated":
            outcome_text = (
                f"{session['addressee_name']} replied but I couldn't pin it down. "
                "Want me to follow up or you take it?"
            )
        elif branch == "4d_no_reply_escalated":
            outcome_text = (
                f"Haven't heard back from {session['addressee_name']}. "
                "Want me to follow up or take it from here?"
            )
        else:
            outcome_text = f"Closed out the {session['addressee_name']} check."

    outcome_provenance = (
        {"llm_call": "compose_coordination_outcome"} if composed.get("text")
        else {"fallback_audit": "2026-05-29"}
    )
    send_result = _send_with_scanner(
        config, bb=bb, text=outcome_text,
        recipient_handle=session["requester_handle"],
        kind="outcome_report",
        provenance=outcome_provenance,
    )
    session["outcome_report_text"] = outcome_text
    # Latency from "resolution event" — for 4a/4b that's when the addressee
    # reply landed; for 4c_escalated / 4d_escalated that's when the
    # escalation decision was made (which is now). Use the addressee-reply
    # ts when present, else now.
    resolution_ts = session.get("addressee_reach_ts") or session.get("inbound_received_ts") or time.time()
    if branch in {"4c_ambiguous_escalated", "4d_no_reply_escalated"}:
        resolution_ts = time.time()
    session["outcome_report_latency_sec"] = max(0, int(time.time() - resolution_ts))
    session["phase"] = "closed"

    _log_phase(config, session=session, phase="outcome_report",
               usage=composed.get("_usage"), closed=True)

    # Drop the session from the in-memory map; idempotency hash already
    # prevents a re-fire from spawning a duplicate. The write-through below
    # persists the removal so a restart does not resurrect a closed session.
    with _sessions_lock:
        _sessions.pop(session["session_id"], None)
    _persist_sessions(config)

    return {
        "status": "coordination_closed",
        "session_id": session["session_id"],
        "branch": branch,
        "task_created": session.get("task_created", False),
        "task_id": session.get("task_id"),
        "outcome_report_text": outcome_text,
    }


def purge_idle_sessions(
    idle_timeout_sec: int = _SESSION_IDLE_TIMEOUT_SEC,
    config: dict | None = None,
) -> int:
    """Drop sessions older than `idle_timeout_sec`. Called by the scheduler
    on a periodic tick (or manually in tests). Returns count purged.

    `config` (added 2026-06-10): when provided, the purge result is written
    through to the persisted session file."""
    _ensure_sessions_loaded(config)
    now = time.monotonic()
    purged = 0
    with _sessions_lock:
        stale = [
            sid for sid, s in _sessions.items()
            if now - s.get("monotonic_started", now) > idle_timeout_sec
        ]
        for sid in stale:
            _sessions.pop(sid, None)
            purged += 1
    if purged:
        _persist_sessions(config)
    return purged


# ---- test helpers (do NOT call from production paths) ----------------------


def _reset_for_tests() -> None:
    """Clear in-memory state between tests. Resetting `_sessions_loaded`
    also lets persistence tests simulate a process restart: reset, then the
    next lookup with a config lazy-loads from disk. Test-only — production
    code should never call this."""
    global _sessions_loaded
    with _sessions_lock:
        _sessions.clear()
        _recent_inbound_hashes.clear()
        _sessions_loaded = False
