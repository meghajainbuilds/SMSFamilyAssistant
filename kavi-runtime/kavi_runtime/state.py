"""Append-only state files. Reads and atomic appends only; never mutates prior rows.

corrections.jsonl: skip-correction events.
runs.jsonl: per-event metrics rows for /metrics dashboard.

State storage (Phase 3, 2026-06-02): the historical monolithic
imessage-state.json has been split into per-concept files (questions.json,
summary_queue.json, pause_state.json, pending_alerts.json,
action_clarifications.json, alert_dedupe.json, self_check.json). See
docs/phase3-concept-inventory.md for the mapping. `load_imessage_state` /
`save_imessage_state` remain as a compatibility surface that dispatches
across the per-concept files transparently — callers that still treat the
state as a single dict keep working with no behavioral change, while data
on disk is sliced. New code should call the concept-specific helpers in
kavi_runtime.state_per_concept directly.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kavi_runtime import state_per_concept as _spc
from kavi_runtime.state_io import atomic_write_json
from kavi_runtime.state_schemas import ImessageState

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_quiet_hours(config: dict, now: datetime | None = None) -> bool:
    """Returns True if local time is within the configured quiet-hours window.
    Single source of truth — used by both scheduler.py and handlers.py."""
    if now is None:
        now = datetime.now(LOCAL_TZ)
    start = time.fromisoformat(config["schedule"]["quiet_hours_start"])
    end = time.fromisoformat(config["schedule"]["quiet_hours_end"])
    t = now.time()
    if start < end:
        return start <= t < end
    return t >= start or t < end


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Atomic append. Each call writes one line."""
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a") as f:
        f.write(line)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def append_correction(path: Path, correction: dict[str, Any]) -> None:
    """Append a correction record. Caller passes the full classifier output plus
    runtime metadata (run_id_at_correction, applied=False). Schema is intentionally
    flexible — new fields just appear in subsequent rows."""
    record = {
        "ts": utc_now_iso(),
        "applied": False,
    }
    record.update(correction)
    append_jsonl(path, record)


def read_unapplied_corrections(path: Path, promoted_path: Path | None = None) -> list[dict[str, Any]]:
    """Correction rows where applied=false AND whose (type, target_pattern) has not
    been promoted to household.md Examples. Promotion supersedes individual application —
    once a pattern is promoted, all corrections matching it are filtered out of injection,
    even if they were never explicitly marked applied. corrections.jsonl stays append-only."""
    if not path.exists():
        return []
    rows = [r for r in read_jsonl(path) if not r.get("applied", False)]
    if promoted_path is None or not promoted_path.exists():
        return rows
    promoted = {(p["type"], p["target_pattern"]) for p in read_jsonl(promoted_path)}
    return [r for r in rows if (r.get("type"), r.get("target_pattern")) not in promoted]


def append_promoted_pattern(path: Path, pattern: dict[str, Any]) -> None:
    """Record that (type, target_pattern) has been promoted to household.md Examples.
    Subsequent reads of unapplied corrections filter these out."""
    record = {"ts": utc_now_iso(), **pattern}
    append_jsonl(path, record)


def read_rejected_patterns(path: Path) -> set[tuple[str, str]]:
    """Set of (type, target_pattern) Megha rejected. Don't re-propose unless re-emerges
    above the after_rejection threshold."""
    if not path.exists():
        return set()
    return {(r["type"], r["target_pattern"]) for r in read_jsonl(path)}


def append_rejected_pattern(path: Path, pattern: dict[str, Any]) -> None:
    record = {"ts": utc_now_iso(), **pattern}
    append_jsonl(path, record)


def append_run(path: Path, run: dict[str, Any]) -> None:
    """Run record schema additions in v0.2: latency_first_action_sec, latency_reply_update_sec.

    Caller is responsible for the full row shape per metrics/definitions.md.
    """
    append_jsonl(path, run)


_DEFAULT_STATE = {
    "questions": [],
    "summary_queue": [],
    "last_send_at": None,
    "last_summary_send_at": None,
    # Step 12 guardrail fields (added 2026-04-29). Backfilled lazily on first read.
    "auto_runs_paused": False,
    "paused_since": None,
    "paused_reason": None,
    "paused_email_queue": [],
    "pending_alerts": [],
    "spend_cap_bypass_month": None,
}

# Phase 3 (2026-06-02): every top-level key in imessage-state.json maps to
# exactly one concept's file. The compatibility layer below uses this mapping
# to dispatch reads and writes across the per-concept files. Keys not listed
# here fall back to the legacy file (forward-compat for future additions).
_KEY_TO_CONCEPT: dict[str, str] = {
    "questions": "questions",
    "last_send_at": "questions",
    "summary_queue": "summary_queue",
    "last_summary_send_at": "summary_queue",
    "last_summary_anchors": "summary_queue",
    "auto_runs_paused": "pause_state",
    "paused_since": "pause_state",
    "paused_reason": "pause_state",
    "paused_email_queue": "pause_state",
    "paused_spend_usd": "pause_state",
    "spend_cap_bypass_month": "pause_state",
    "pending_alerts": "pending_alerts",
    "pending_action_clarifications": "action_clarifications",
    "alert_dedupe": "alert_dedupe",
    "last_failure_rate_alert_at": "alert_dedupe",
    "pending_self_check": "self_check",
}

_CONCEPT_LOADERS = {
    "questions": _spc.load_questions,
    "summary_queue": _spc.load_summary_queue,
    "pause_state": _spc.load_pause_state,
    "pending_alerts": _spc.load_pending_alerts,
    "action_clarifications": _spc.load_action_clarifications,
    "alert_dedupe": _spc.load_alert_dedupe,
    "self_check": _spc.load_self_check,
}

_CONCEPT_SAVERS = {
    "questions": _spc.save_questions,
    "summary_queue": _spc.save_summary_queue,
    "pause_state": _spc.save_pause_state,
    "pending_alerts": _spc.save_pending_alerts,
    "action_clarifications": _spc.save_action_clarifications,
    "alert_dedupe": _spc.save_alert_dedupe,
    "self_check": _spc.save_self_check,
}


def _load_legacy_only(path: Path) -> dict[str, Any]:
    """Read the legacy monolithic file directly. Used during/post migration
    transitions and as a fallback for any keys not yet sliced into a
    per-concept file (forward-compat)."""
    if not path.exists():
        return {}
    raw = path.read_text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Best-effort recovery: extract the first valid JSON object and
        # archive the corrupt file. Mirrors the pre-Phase-3 behavior.
        try:
            recovered, end = json.JSONDecoder().raw_decode(raw)
            archive = path.with_suffix(path.suffix + f".corrupt-{utc_now_iso().replace(':', '')}")
            archive.write_text(raw)
            logger.critical(
                "legacy imessage-state.json corrupted (%d valid bytes + %d trailing); "
                "recovered first valid object, archived original to %s. Error: %s",
                end, len(raw) - end, archive.name, e,
            )
            return recovered
        except Exception as recovery_err:
            logger.critical(
                "legacy imessage-state.json unrecoverable (%s); returning empty.",
                recovery_err,
            )
            return {}


def load_imessage_state(path: Path) -> dict[str, Any]:
    """Read the runtime's iMessage state as a single merged dict.

    Phase 3 (2026-06-02): data is stored per-concept on disk, but this
    function merges every concept's slice into one dict for callers that
    still treat the state as monolithic. Any keys still present in the
    legacy file (forward-compat) merge on top.

    Returns a fresh dict the caller may mutate freely. Concept-level
    corruption is handled inside each per-concept loader (archive + return
    defaults); legacy-file corruption is handled here.
    """
    state: dict[str, Any] = dict(_DEFAULT_STATE)

    # Layer 1: per-concept files (the new canonical home).
    for loader in _CONCEPT_LOADERS.values():
        try:
            slice_data = loader(path)
        except Exception as e:  # noqa: BLE001 - never let one slice break the whole read
            logger.critical("per-concept loader failed; continuing with defaults: %s", e)
            continue
        state.update(slice_data)

    # Layer 2: anything still in the legacy file (e.g., not yet migrated, or
    # a forward-compat key). Per-concept data takes precedence for any key
    # the slicer claims; legacy fills in the rest.
    legacy = _load_legacy_only(path)
    for k, v in legacy.items():
        # If this key isn't claimed by any concept, copy it through so
        # callers reading unknown fields still get them.
        if k not in _KEY_TO_CONCEPT:
            state[k] = v
        # If this key IS claimed but the per-concept file was missing (so
        # the concept slice has the default), prefer the legacy value. This
        # is the transient state between deploy and the boot migration
        # completing — the migration normally closes this gap on first boot.
        else:
            concept = _KEY_TO_CONCEPT[k]
            concept_path = _spc.CONCEPTS[concept][0](path)
            if not concept_path.exists():
                state[k] = v

    return state


def save_imessage_state(path: Path, state: dict[str, Any]) -> None:
    """Write a merged-dict state by dispatching each key to its owning
    per-concept file. Phase 3 compatibility shim.

    The Pydantic ImessageState validator still runs against the merged
    input so callers can't slip in malformed shapes (e.g., questions as a
    dict instead of a list).
    """
    # Validate the merged shape exactly as the pre-Phase-3 code did.
    ImessageState.model_validate(state)

    # Group keys by concept, then save each concept atomically.
    by_concept: dict[str, dict[str, Any]] = {c: {} for c in _CONCEPT_SAVERS}
    unowned: dict[str, Any] = {}
    for k, v in state.items():
        concept = _KEY_TO_CONCEPT.get(k)
        if concept is None:
            unowned[k] = v
        else:
            by_concept[concept][k] = v

    # For each concept, merge what the caller wrote with what's already
    # on disk so a caller that only mutated a subset of keys doesn't
    # accidentally zero-out the other keys in the same concept.
    for concept, write_slice in by_concept.items():
        if not write_slice:
            continue
        loader = _CONCEPT_LOADERS[concept]
        saver = _CONCEPT_SAVERS[concept]
        current = loader(path)
        current.update(write_slice)
        saver(path, current)

    # Forward-compat: persist any unowned keys back to the legacy file if
    # it still exists. New keys added by future code without an updated
    # concept mapping should still survive a write.
    if unowned and path.exists():
        legacy = _load_legacy_only(path)
        legacy.update(unowned)
        atomic_write_json(path, legacy)


def add_pending_question(path: Path, question: dict[str, Any]) -> None:
    """Append a pending question to questions.json (direct per-concept call,
    Phase 5 2026-06-02; formerly routed through the load/save_imessage_state
    shim)."""
    qstate = _spc.load_questions(path)
    qstate.setdefault("questions", []).append(question)
    qstate["last_send_at"] = utc_now_iso()
    _spc.save_questions(path, qstate)


def pop_pending_question(path: Path, index: int) -> dict[str, Any] | None:
    qstate = _spc.load_questions(path)
    qs = qstate.get("questions", [])
    if index < 0 or index >= len(qs):
        return None
    q = qs.pop(index)
    qstate["questions"] = qs
    _spc.save_questions(path, qstate)
    return q


def list_pending_questions(path: Path) -> list[dict[str, Any]]:
    return _spc.load_questions(path).get("questions", [])


# A close-or-drop offer Kavi surfaces in the morning digest (stale-task nudge,
# evening close suggestion) registers a bindable pending question so a later
# bare "keep"/"drop" resolves it through the qa executor. Unlike a low-conf
# inbox question (which persists until answered), an unanswered nudge must not
# pile up: it carries an expires_at and the reply read-path drops it once past.
# 18h survives the day (an afternoon "keep" still binds) but is gone by the
# next morning's digest so the same offer never resurfaces stale.
STALE_NUDGE_QUESTION_TTL_SECONDS = 18 * 60 * 60


def drop_expired_questions(
    questions: list[dict[str, Any]], *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter out pending questions whose `expires_at` is in the past.

    Entries WITHOUT an `expires_at` field (e.g. low-conf inbox questions,
    which live until answered) are always kept — expiry is opt-in per entry,
    so this is backward-compatible with every existing question shape. A
    malformed `expires_at` is treated as expired (fail-closed: a question we
    can't time-bound shouldn't bind a reply forever).
    """
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for q in questions:
        exp = q.get("expires_at")
        if not exp:
            out.append(q)
            continue
        try:
            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if now < exp_dt:
            out.append(q)
    return out


def enqueue_summary_item(path: Path, item: dict[str, Any]) -> None:
    """Add a high-conf, non-priority task to the summary queue. Surfaces in the
    next scheduled periodic_summary as a 'tasks added' line.

    Direct summary_queue.json call (Phase 5 2026-06-02)."""
    sq = _spc.load_summary_queue(path)
    sq.setdefault("summary_queue", []).append(item)
    _spc.save_summary_queue(path, sq)


def drain_summary_queue(path: Path) -> list[dict[str, Any]]:
    """Pop all items from the summary queue. Caller is responsible for sending
    them; if the send fails, items are lost. Acceptable trade-off for v0.2 — a
    re-queue path adds complexity for a low-impact failure mode.

    Direct summary_queue.json call (Phase 5 2026-06-02)."""
    sq = _spc.load_summary_queue(path)
    items = sq.get("summary_queue", [])
    sq["summary_queue"] = []
    sq["last_summary_send_at"] = utc_now_iso()
    _spc.save_summary_queue(path, sq)
    return items


# ---- pending action clarifications -----------------------------------------
#
# Why: 2026-05-07. The action layer is single-turn stateless. When Kavi shipped
# a clarifying reply ("Two UW tasks: bill + $630 balance — mark both?"),
# Megha's "Yes" reply was classified independently and routed to the
# conversational path. No mark_task_done fired. These helpers persist the
# proposal so the next inbound from the same sender can resolve it.

PENDING_CLARIFICATION_TTL_SECONDS = 10 * 60


def save_pending_clarification(
    path: Path,
    sender_handle: str,
    *,
    action_type: str,
    original_inbound: str,
    proposed_matches: list[dict[str, Any]],
    reply_sent: str,
    now: datetime | None = None,
) -> None:
    """Persist a pending clarification keyed on the sender handle. Overwrites
    any prior pending entry for the same sender — only the most-recent ask is
    valid. `proposed_matches` is a list of {id, title, confidence} dicts
    grounded in the open task list at compose time.
    """
    if not sender_handle:
        return
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(seconds=PENDING_CLARIFICATION_TTL_SECONDS)
    ac = _spc.load_action_clarifications(path)
    pending = ac.setdefault("pending_action_clarifications", {})
    pending[sender_handle] = {
        "set_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action_type": action_type,
        "original_inbound": original_inbound,
        "proposed_matches": proposed_matches,
        "reply_sent": reply_sent,
    }
    _spc.save_action_clarifications(path, ac)


def load_pending_clarification(
    path: Path,
    sender_handle: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the active pending clarification for `sender_handle`, or None
    if there isn't one or the saved entry has expired. Expired entries are
    cleared from disk on read so subsequent calls don't re-evaluate.
    """
    if not sender_handle:
        return None
    ac = _spc.load_action_clarifications(path)
    pending = ac.get("pending_action_clarifications") or {}
    entry = pending.get(sender_handle)
    if not entry:
        return None
    now = now or datetime.now(timezone.utc)
    expires_str = entry.get("expires_at") or ""
    try:
        expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        # Malformed expiry — treat as expired and clear.
        clear_pending_clarification(path, sender_handle)
        return None
    if now >= expires_dt:
        clear_pending_clarification(path, sender_handle)
        return None
    return entry


def clear_pending_clarification(path: Path, sender_handle: str) -> None:
    """Remove the pending clarification for `sender_handle`. No-op if no
    entry exists. Called after the resolver decides (execute / ignore /
    fresh_intent) so the same proposal isn't matched against unrelated
    later inbounds.
    """
    if not sender_handle:
        return
    ac = _spc.load_action_clarifications(path)
    pending = ac.get("pending_action_clarifications") or {}
    if sender_handle in pending:
        del pending[sender_handle]
        ac["pending_action_clarifications"] = pending
        _spc.save_action_clarifications(path, ac)


# ---- summary-anchor state (Fix 2, 2026-05-07) -----------------------------
#
# Why: when periodic_summary mentions ONE specific task and Megha replies
# "Yes on [topic]", the action layer's matcher gets several candidates
# (multiple Elders' Tea tasks open) and dumps a flat clarification with the
# wrong count. By saving the task the summary anchored on, the action layer
# can pick the anchor as the primary mark_done and offer siblings as a
# sweep instead.

SUMMARY_ANCHOR_TTL_SECONDS = 30 * 60


def save_summary_anchor(
    path: Path,
    recipient_handle: str,
    *,
    anchor_task_id: str,
    anchor_task_title: str,
    summary_message: str,
    now: datetime | None = None,
) -> None:
    """Persist the anchor task that the most-recent periodic_summary
    mentioned for `recipient_handle`. Overwrites any prior entry — only the
    latest summary's anchor is valid. Caller passes the chosen anchor (today
    the highest-priority queued task, or the first pending Q&A if no queued
    tasks). Cleared on read past TTL or after the action layer consumes it.
    """
    if not recipient_handle or not anchor_task_id:
        return
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(seconds=SUMMARY_ANCHOR_TTL_SECONDS)
    sq = _spc.load_summary_queue(path)
    anchors = sq.setdefault("last_summary_anchors", {})
    anchors[recipient_handle] = {
        "set_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "anchor_task_id": anchor_task_id,
        "anchor_task_title": anchor_task_title,
        "summary_message": (summary_message or "")[:500],
    }
    _spc.save_summary_queue(path, sq)


def load_summary_anchor(
    path: Path,
    recipient_handle: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the active summary anchor for `recipient_handle`, or None if
    there isn't one or the saved entry has expired. Expired entries are
    scrubbed on read."""
    if not recipient_handle:
        return None
    sq = _spc.load_summary_queue(path)
    anchors = sq.get("last_summary_anchors") or {}
    entry = anchors.get(recipient_handle)
    if not entry:
        return None
    now = now or datetime.now(timezone.utc)
    expires_str = entry.get("expires_at") or ""
    try:
        expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        clear_summary_anchor(path, recipient_handle)
        return None
    if now >= expires_dt:
        clear_summary_anchor(path, recipient_handle)
        return None
    return entry


def clear_summary_anchor(path: Path, recipient_handle: str) -> None:
    """Remove the summary anchor for `recipient_handle`. No-op if no entry
    exists. Called after the action layer consumes the anchor so a later
    unrelated reply doesn't anchor against a stale task."""
    if not recipient_handle:
        return
    sq = _spc.load_summary_queue(path)
    anchors = sq.get("last_summary_anchors") or {}
    if recipient_handle in anchors:
        del anchors[recipient_handle]
        sq["last_summary_anchors"] = anchors
        _spc.save_summary_queue(path, sq)
