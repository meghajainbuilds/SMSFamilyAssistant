"""Weekly self-check (Stage 4 system-level value metric).

Friday 14:00 Pacific: Kavi LLM-composes a cognitive-load question via persona prompt
(skills/weekly_self_check_composer.md), iMessages Megha, and stores a pending state.

When Megha replies (within the timeout window): handlers.imessage_received calls
`try_handle_self_check_reply` BEFORE falling through to correction-classifier routing.
The reply is classified by Sonnet (skills/weekly_self_check_classifier.md) into
{saved, added, neutral, unclear} and logged to eval-persona-weekly-self-check.jsonl.

Saturday 14:00 Pacific (24h after send): if the pending state is still set, write a
no_reply row and clear the pending state.

Spec: capabilities/kavi-persona.md → Metrics → System-level value.
Schema: evals/definitions.md → eval-persona-weekly-self-check.jsonl.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kavi_runtime.state import (
    append_jsonl,
    utc_now_iso,
)
from kavi_runtime.state_per_concept import (
    load_self_check,
    save_self_check,
)

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_TIMEOUT_HOURS = 24


def _eval_self_check_path(config: dict) -> Path:
    """Path to eval-persona-weekly-self-check.jsonl on Kavi. Create parent dir if missing."""
    p = Path(config["paths"]["eval_persona_weekly_self_check_jsonl"])
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _state_path(config: dict) -> Path:
    return Path(config["paths"]["imessage_state"])


def _get_pending(state: dict[str, Any]) -> dict[str, Any] | None:
    return state.get("pending_self_check")


def _set_pending(state: dict[str, Any], pending: dict[str, Any] | None) -> None:
    if pending is None:
        state.pop("pending_self_check", None)
    else:
        state["pending_self_check"] = pending


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _is_expired(pending: dict[str, Any], now_utc: datetime | None = None) -> bool:
    expires_at = pending.get("expires_at")
    if not expires_at:
        return True
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    return now_utc >= _parse_iso(expires_at)


def send_weekly_self_check(config: dict) -> dict[str, Any]:
    """Friday 2 PM cron entry. Wraps the body in a Trace context so the
    compose_weekly_self_check LLM call + outbound iMessage land in
    evals/traces/exchanges.jsonl as a scheduler-channel exchange. Failure-safe
    same as the rest of the Phase B instrumentation — a Trace construction or
    write failure must never break self-check sending. See
    _send_weekly_self_check_impl for the full behavior.
    """
    try:
        from kavi_runtime.trace_log import Trace as _Trace
        _trace_ctx = _Trace(
            channel="scheduler", config=config, capability="kavi-persona",
        )
    except Exception as _e:
        logger.debug("trace_log: Trace construction failed (continuing): %s", _e)
        _trace_ctx = None
    if _trace_ctx is None:
        return _send_weekly_self_check_impl(config)
    with _trace_ctx:
        return _send_weekly_self_check_impl(config)


def _send_weekly_self_check_impl(config: dict) -> dict[str, Any]:
    """Friday cron entry. Compose question via Sonnet, send iMessage, persist pending state.

    No-op if a pending self-check is already set and not yet expired (defensive — guards
    against duplicate cron fires or manual replays).
    """
    from kavi_runtime.handlers import _get_clients, _send_imessage_with_fallback

    state_path = _state_path(config)
    state = load_self_check(state_path)
    existing = _get_pending(state)
    if existing and not _is_expired(existing):
        logger.info("send_weekly_self_check: already pending (sent %s, expires %s); skipping",
                    existing.get("ts"), existing.get("expires_at"))
        return {"status": "skipped", "reason": "already_pending"}

    _, claude, _ = _get_clients(config)
    question = claude.compose_weekly_self_check()
    if not question:
        logger.warning("send_weekly_self_check: composer returned empty; skipping send (no fallback "
                       "for weekly self-check — better to miss a week than send a templated question)")
        return {"status": "error", "reason": "composer_failed"}

    send_result = _send_imessage_with_fallback(
        config, question, kind="weekly_self_check",
        provenance={"llm_call": "compose_weekly_self_check"},
    )

    timeout_hours = config.get("schedule", {}).get("weekly_self_check_timeout_hours", DEFAULT_TIMEOUT_HOURS)
    sent_at_utc = datetime.now(timezone.utc)
    expires_at_utc = sent_at_utc + timedelta(hours=timeout_hours)

    pending = {
        "ts": sent_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "question": question,
        "send_kind": send_result.get("kind", "weekly_self_check"),
        "fallback_used": send_result.get("fallback_used", False),
    }
    _set_pending(state, pending)
    save_self_check(state_path, state)
    logger.info("send_weekly_self_check: sent question=%r expires_at=%s",
                question[:80], pending["expires_at"])
    return {"status": "sent", "pending": pending, "send_result": send_result}


def try_handle_self_check_reply(text: str, config: dict) -> dict[str, Any] | None:
    """Inbound-iMessage hook. If a self-check is pending and not expired, classify
    `text` via Sonnet. If classifier says it's a self-check reply: log + clear pending,
    return a status dict. Otherwise: return None so the caller routes to existing
    correction/QA handling (we never block normal routing on a self-check ambiguity).

    Returns:
      dict   — handled as self-check reply (caller should return this).
      None   — not a self-check reply, fall through to existing routing.
    """
    state_path = _state_path(config)
    state = load_self_check(state_path)
    pending = _get_pending(state)
    if not pending:
        return None
    if _is_expired(pending):
        logger.info("try_handle_self_check_reply: pending expired during inbound; clearing without log "
                    "(expire_pending_self_check job handles the no_reply row)")
        return None

    from kavi_runtime.handlers import _get_clients, _send_imessage_with_fallback

    _, claude, _ = _get_clients(config)
    classification = claude.classify_self_check_reply(text, pending.get("question") or "")
    if not classification.get("is_self_check_reply"):
        logger.info("try_handle_self_check_reply: classifier says not a self-check reply; falling through "
                    "rationale=%s", (classification.get("rationale") or "")[:120])
        return None

    rating = classification.get("rating")
    if rating not in {"saved", "added", "neutral", "unclear"}:
        logger.warning("try_handle_self_check_reply: bad rating %r; treating as unclear", rating)
        rating = "unclear"

    sent_iso = pending.get("ts")
    now_utc = datetime.now(timezone.utc)
    latency_sec: int | None = None
    if sent_iso:
        try:
            latency_sec = int((now_utc - _parse_iso(sent_iso)).total_seconds())
        except Exception:
            latency_sec = None

    record = {
        "ts": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capability": "kavi-persona",
        "question": pending.get("question"),
        "reply": text,
        "rating": rating,
        "reply_received": True,
        "reply_latency_sec": latency_sec,
        "rationale": (classification.get("rationale") or "")[:300],
        "usage": classification.get("_usage"),
    }
    append_jsonl(_eval_self_check_path(config), record)
    _set_pending(state, None)
    save_self_check(state_path, state)

    # Conformance-sweep finding (2026-06-10 provenance grandfather pass):
    # deterministic ack that was neither LLM-composed nor previously
    # audited. AUDIT 2026-06-10: honest single sentence; "Logged" is
    # grounded — the eval row append happens directly above and an append
    # failure raises before this send. Candidate for LLM migration if the
    # self-check ack ever needs to carry content.
    ack = "Got it. Logged — thanks for the read."
    _send_imessage_with_fallback(
        config, ack, kind="self_check_ack",
        provenance={"fallback_audit": "2026-06-10"},
    )
    logger.info("try_handle_self_check_reply: rating=%s latency_sec=%s", rating, latency_sec)
    return {"status": "self_check_logged", "rating": rating, "latency_sec": latency_sec}


def expire_pending_self_check(config: dict) -> dict[str, Any]:
    """Saturday cron entry (24h after Friday send). If pending still set, write
    no_reply row and clear. No-op otherwise. Idempotent."""
    state_path = _state_path(config)
    state = load_self_check(state_path)
    pending = _get_pending(state)
    if not pending:
        return {"status": "no_pending"}
    if not _is_expired(pending):
        logger.info("expire_pending_self_check: pending exists but not yet expired (%s); skipping",
                    pending.get("expires_at"))
        return {"status": "not_expired"}

    sent_iso = pending.get("ts")
    record = {
        "ts": utc_now_iso(),
        "capability": "kavi-persona",
        "question": pending.get("question"),
        "reply": None,
        "rating": "no_reply",
        "reply_received": False,
        "reply_latency_sec": None,
        "rationale": None,
        "usage": None,
    }
    append_jsonl(_eval_self_check_path(config), record)
    _set_pending(state, None)
    save_self_check(state_path, state)
    logger.info("expire_pending_self_check: logged no_reply for ts=%s", sent_iso)
    return {"status": "logged_no_reply", "ts": sent_iso}
