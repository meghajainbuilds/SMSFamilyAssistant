"""Handler-level alerting for the runtime.

Closes the 2026-05-06 silent-degradation gap: process is up + Healthchecks
heartbeat is firing, but downstream handlers (persona LLM call, Anthropic API)
are failing. The user (Megha) sent three test iMessages back-to-back during an
Anthropic 529 wave; all three failed at the persona composer; she got no
signal at all.

This module fires three alert paths that Healthchecks.io can't:

  Signal 1 — Per-failure email to Megha when an iMessage from a household
             member raises after retries. NO LLM call.
  Signal 2 — Hand-coded fallback iMessage to the same sender on the same
             failure. NO LLM call. Goes through the canonical send wrapper
             (recipient + content gates still enforced).
  Signal 3 — Rolling failure-rate alert. If handler_failures /
             handler_invocations crosses 30% in a 5-minute window, fire
             ONE summary email. Debounced 30 min.

All three reuse `graph_client.send_mail` (added 2026-04-29 for the Outlook
silent-send fallback). No new MS Graph endpoint surface is introduced.

The alert path is best-effort: any exception inside this module is logged
and swallowed. A failed alert must NEVER mask the original handler exception
or block the webhook 202 response.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kavi_runtime import household as _household
from kavi_runtime.state_io import atomic_write_json

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# ---- error classification --------------------------------------------------

ERROR_CLASS_OVERLOADED = "Anthropic overloaded"
ERROR_CLASS_STATE_CORRUPTION = "state corruption"
ERROR_CLASS_UNKNOWN = "unknown"


def classify_exception(exc: BaseException) -> str:
    """Map an exception to one of three human-readable classes used in the
    alert email Subject/Body. Conservative on the matching side: a 529 string
    in the message OR an `OverloadedError` type both map to overloaded.
    """
    type_name = type(exc).__name__
    msg = str(exc)
    if type_name == "OverloadedError" or "529" in msg or "overloaded_error" in msg.lower():
        return ERROR_CLASS_OVERLOADED
    if isinstance(exc, json.JSONDecodeError):
        return ERROR_CLASS_STATE_CORRUPTION
    return ERROR_CLASS_UNKNOWN


# ---- rolling failure-rate counter ------------------------------------------

# In-memory rolling-window counter. Bucket = (epoch_minute). Reset on runtime
# restart by design — restart implies operator intervention; old data is no
# longer representative of "what's happening right now".
_RATE_LOCK = threading.Lock()
_RATE_INVOCATIONS: dict[int, int] = defaultdict(int)
_RATE_FAILURES: dict[int, int] = defaultdict(int)
_RATE_FAILURES_BY_CLASS: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_LAST_SUCCESS_AT: dict[str, float] = {}  # handler_name -> epoch seconds
_LAST_INVOCATION_AT: dict[str, float] = {}  # handler_name -> epoch (success OR fail)

# Window + threshold parameters. Constants, not config-driven for v0; the
# whole point is "Megha needs a signal she's not getting today."
WINDOW_MINUTES = 5
FAILURE_RATE_THRESHOLD = 0.30
ALERT_DEBOUNCE_PER_KEY_SEC = 5 * 60        # Signals 1+2 per (sender, error_class)
RATE_ALERT_DEBOUNCE_SEC = 30 * 60          # Signal 3
#
# Removed 2026-05-26: the 60-min "no activity" iMessage alert path (a.k.a.
# invocation-floor alarm). Silence is not failure. Over 7 days that path
# generated 75 of 89 persona-eval rows and 11 of 84 labeled rubric sessions,
# all false positives. The runtime no longer pushes ops-health into Megha's
# iMessage thread with Kavi. Pull-based status lives at GET /status; real
# silent failures are caught by the external healthchecks.io watchdog.
# Signals 1, 2, and 3 (per-failure alert, fallback iMessage, rolling rate
# alert) are unchanged. See kavi-runtime/backlog.md "Runtime heartbeat"
# entry for the full rationale.


def record_invocation(handler_name: str) -> None:
    """Increment the rolling invocation counter and stamp last-invocation time."""
    now = time.time()
    bucket = int(now // 60)
    with _RATE_LOCK:
        _RATE_INVOCATIONS[bucket] += 1
        _LAST_INVOCATION_AT[handler_name] = now
        _prune_buckets_locked(bucket)


def record_success(handler_name: str) -> None:
    """Record the timestamp of the last successful invocation per handler.
    Used in the Signal 3 email body for context ("last success was <T>")."""
    with _RATE_LOCK:
        _LAST_SUCCESS_AT[handler_name] = time.time()


def record_failure(handler_name: str, error_class: str) -> None:
    """Increment the rolling failure counters for the current minute bucket."""
    bucket = int(time.time() // 60)
    with _RATE_LOCK:
        _RATE_FAILURES[bucket] += 1
        _RATE_FAILURES_BY_CLASS[bucket][error_class] += 1
        _prune_buckets_locked(bucket)


def _prune_buckets_locked(now_bucket: int) -> None:
    """Drop buckets older than WINDOW_MINUTES. Caller must hold _RATE_LOCK."""
    cutoff = now_bucket - WINDOW_MINUTES + 1
    for d in (_RATE_INVOCATIONS, _RATE_FAILURES):
        stale = [b for b in d if b < cutoff]
        for b in stale:
            del d[b]
    stale_cls = [b for b in _RATE_FAILURES_BY_CLASS if b < cutoff]
    for b in stale_cls:
        del _RATE_FAILURES_BY_CLASS[b]


def current_window_stats() -> dict[str, Any]:
    """Snapshot of the last WINDOW_MINUTES of handler activity. Used by
    Signal 3 to compose the body and decide whether to fire."""
    now_bucket = int(time.time() // 60)
    with _RATE_LOCK:
        _prune_buckets_locked(now_bucket)
        invocations = sum(_RATE_INVOCATIONS.values())
        failures = sum(_RATE_FAILURES.values())
        by_class: dict[str, int] = defaultdict(int)
        for bucket_classes in _RATE_FAILURES_BY_CLASS.values():
            for cls, n in bucket_classes.items():
                by_class[cls] += n
        last_success = dict(_LAST_SUCCESS_AT)
    rate = (failures / invocations) if invocations else 0.0
    return {
        "invocations": invocations,
        "failures": failures,
        "rate": rate,
        "by_class": dict(by_class),
        "last_success_at": last_success,
        "window_minutes": WINDOW_MINUTES,
    }


def reset_for_test() -> None:
    """Test-only: clear all in-memory counters between tests. Production
    callers must never invoke this; the module-level state is the rolling
    window, not a cache."""
    with _RATE_LOCK:
        _RATE_INVOCATIONS.clear()
        _RATE_FAILURES.clear()
        _RATE_FAILURES_BY_CLASS.clear()
        _LAST_SUCCESS_AT.clear()
        _LAST_INVOCATION_AT.clear()


# ---- dedupe state on alert_dedupe.json -------------------------------------

# Phase 3 (2026-06-02) moved alert_dedupe + last_failure_rate_alert_at off
# the monolithic imessage-state.json onto its own per-concept file at
# alert_dedupe.json. Phase 5 (same day) rewired the read/write paths below
# from `_load_state(imessage_state_path)` -> `load_alert_dedupe` /
# `save_alert_dedupe`. The legacy `_load_state` / `_save_state` helpers
# were silently writing to a path that no longer exists after the Phase 3
# rename to .pre-phase3-backup; in production this meant the dedupe was
# always reading {} and double-alerting on every same-class failure.
# Fixed inline as part of Phase 5.
#
# Two fields persisted:
#   alert_dedupe: dict[str, float]      # key = "<sender>|<error_class>",
#                                       # value = epoch sec of last alert email
#   last_failure_rate_alert_at: float   # epoch sec of last Signal 3 email
#
# Keys are kept short and bounded (oldest entries pruned on each write).

from kavi_runtime.state_per_concept import (
    load_alert_dedupe as _load_alert_dedupe,
    save_alert_dedupe as _save_alert_dedupe,
)

_ALERT_DEDUPE_KEY = "alert_dedupe"
_RATE_ALERT_KEY = "last_failure_rate_alert_at"
_DEDUPE_MAX_KEYS = 64  # plenty of headroom; usual case is 1-2


def _should_fire_per_sender_alert(state_path: Path, sender: str, error_class: str) -> bool:
    """Returns True if no alert email for (sender, error_class) has fired in
    the last ALERT_DEBOUNCE_PER_KEY_SEC window. Updates the persisted dedupe
    record on True. Reads/writes alert_dedupe.json directly (Phase 5)."""
    state = _load_alert_dedupe(state_path)
    dedupe = state.get(_ALERT_DEDUPE_KEY, {}) or {}
    key = f"{sender}|{error_class}"
    now = time.time()
    last = dedupe.get(key)
    if last is not None and (now - float(last)) < ALERT_DEBOUNCE_PER_KEY_SEC:
        return False
    # Prune entries older than the window so the dict doesn't grow.
    dedupe = {
        k: v for k, v in dedupe.items()
        if (now - float(v)) < ALERT_DEBOUNCE_PER_KEY_SEC
    }
    dedupe[key] = now
    # Bound the dict size as a belt-and-suspenders guard against a pathological
    # outage that produces many sender/class combinations in 5 minutes.
    if len(dedupe) > _DEDUPE_MAX_KEYS:
        sorted_items = sorted(dedupe.items(), key=lambda kv: kv[1], reverse=True)
        dedupe = dict(sorted_items[:_DEDUPE_MAX_KEYS])
    state[_ALERT_DEDUPE_KEY] = dedupe
    _save_alert_dedupe(state_path, state)
    return True


def _should_fire_rate_alert(state_path: Path) -> bool:
    """Returns True if no failure-rate alert has fired in the last
    RATE_ALERT_DEBOUNCE_SEC window. Updates the persisted timestamp on True.
    Reads/writes alert_dedupe.json directly (Phase 5)."""
    state = _load_alert_dedupe(state_path)
    last = state.get(_RATE_ALERT_KEY)
    now = time.time()
    if last is not None and (now - float(last)) < RATE_ALERT_DEBOUNCE_SEC:
        return False
    state[_RATE_ALERT_KEY] = now
    _save_alert_dedupe(state_path, state)
    return True


# ---- email body composition ------------------------------------------------


def _now_pt_hhmm() -> str:
    return datetime.now(LOCAL_TZ).strftime("%H:%M PT")


def _now_pt_full() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_last_success(epoch: float | None) -> str:
    if not epoch:
        return "never (this runtime instance)"
    dt = datetime.fromtimestamp(epoch, tz=LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def compose_sender_alert_email(
    *, sender_handle: str, error_class: str, retries_attempted: int,
    last_error: str,
) -> tuple[str, str]:
    """Signal 1 email shape. Returns (subject, body).

    Plain-language format (Issue 1 of 2026-05-07 alert-email rewrite):
    leads with the user-visible failure, not the engineering symptom.
    Uses the WHAT BROKE / WHAT STILL WORKS / WHAT THIS AFFECTS / WHAT TO
    DO shape from CLAUDE.md so Megha can triage in under 30 seconds. The
    technical detail block stays at the bottom for whoever debugs.
    """
    pt = _now_pt_hhmm()
    subject = f"Kavi couldn't reply to your {pt} iMessage"
    truncated = (last_error or "")[:200]
    body = (
        "Hi Megha,\n\n"
        "WHAT BROKE\n"
        f"At {pt} you sent Kavi an iMessage from {sender_handle} and he sent "
        "back the canned \"I'm degraded right now\" reply instead of a real "
        "one. The handler that processes inbound iMessages crashed before it "
        "could compose anything. Code or runtime issue, not Microsoft.\n\n"
        "WHAT STILL WORKS\n"
        "- New tasks from your inbox: still being created normally\n"
        "- Email scanning: continues running on schedule\n"
        "- Outbound iMessages from Kavi to you (e.g. periodic summaries): "
        "separate path, runs on schedule\n\n"
        "WHAT THIS AFFECTS\n"
        "- This iMessage did not get a real reply\n"
        "- If the same crash class repeats, more replies will fall through "
        "until a fix ships\n\n"
        "WHAT TO DO\n"
        "- For now: try sending the same iMessage again in a few minutes; "
        "transient errors clear on their own\n"
        "- If it keeps failing: a developer needs to look at the err log "
        "(technical detail at the bottom)\n\n"
        f"Sent by Kavi's alert system at {_now_pt_full()}.\n\n"
        "---\n"
        "Technical detail (for the developer, not for triage):\n"
        f"  Error class: {error_class}\n"
        f"  Retries attempted: {retries_attempted}\n"
        f"  Last error: {truncated}\n"
        f"  Err log: ssh {_household.kavi_ssh_host()} "
        "\"tail -100 /Users/kavi/Library/Logs/kavi-runtime.err.log\"\n"
    )
    return subject, body


def compose_rate_alert_email(stats: dict[str, Any]) -> tuple[str, str]:
    """Signal 3 email shape. Returns (subject, body).

    Plain-language format (Issue 1 of 2026-05-07 alert-email rewrite).
    Same WHAT/WHAT/WHAT/WHAT shape as compose_sender_alert_email; engineering
    detail isolated at the bottom under a "Technical detail" divider.
    """
    pct = int(round(stats["rate"] * 100))
    threshold_pct = int(FAILURE_RATE_THRESHOLD * 100)
    window_min = stats["window_minutes"]
    subject = (
        f"Kavi crashing more than usual: {pct}% of iMessages "
        f"in the last {window_min} min"
    )
    by_class = stats.get("by_class", {})
    if by_class:
        breakdown_lines = [f"  - {cls}: {n}" for cls, n in sorted(by_class.items())]
        breakdown = "\n".join(breakdown_lines)
    else:
        breakdown = "  (none classified)"
    last_success_lines = []
    for handler_name, epoch in sorted(stats.get("last_success_at", {}).items()):
        last_success_lines.append(f"  - {handler_name}: {_format_last_success(epoch)}")
    last_success_block = (
        "\n".join(last_success_lines)
        or "  (no successful invocations recorded)"
    )
    body = (
        "Hi Megha,\n\n"
        "WHAT BROKE\n"
        f"Kavi's iMessage handlers have been crashing more than {threshold_pct}% "
        f"of the time over the last {window_min} minutes. Out of "
        f"{stats['invocations']} attempts, {stats['failures']} failed "
        f"({pct}%).\n\n"
        "WHAT STILL WORKS\n"
        "- New tasks from your inbox: still being created normally "
        "(separate path)\n"
        "- Outbound iMessages from Kavi to you (periodic summaries, "
        "scheduled reminders): separate path, runs on schedule\n\n"
        "WHAT THIS AFFECTS\n"
        "- Replies to your iMessages may continue to fail at the same rate\n"
        "- Anything Kavi was waiting on you for stays pending until handlers "
        "recover\n\n"
        "WHAT TO DO\n"
        f"- For now: assume any iMessage you sent in the last {window_min} "
        "min may not have been processed; check MS To Do for relevant items\n"
        "- For the fix: a developer needs to look at the failure breakdown "
        "below\n\n"
        f"Sent by Kavi's alert system at {_now_pt_full()}. Will not re-fire "
        f"for {RATE_ALERT_DEBOUNCE_SEC // 60} minutes.\n\n"
        "---\n"
        "Technical detail (for the developer):\n"
        "  Failures by error class:\n"
        f"{breakdown}\n"
        "  Last successful invocation per handler:\n"
        f"{last_success_block}\n"
        f"  Err log: ssh {_household.kavi_ssh_host()} "
        "\"tail -100 /Users/kavi/Library/Logs/kavi-runtime.err.log\"\n"
    )
    return subject, body


# ---- public alert dispatch -------------------------------------------------


def maybe_alert_failed_handler(
    *,
    config: dict,
    sender_handle: str | None,
    exc: BaseException,
    retries_attempted: int,
) -> dict[str, bool]:
    """Signal 1 + Signal 2 entry point. Called from the wrapped handler when
    a household-member iMessage handler raises. Returns a dict describing
    what actually happened (used by tests + ops logs). NEVER raises.

    Action sequence:
      1. Resolve sender → if not in HOUSEHOLD_HANDLES, return early. The
         inbound gate already discarded non-household sender events; this
         is a defense-in-depth check in case the wrap order ever changes.
      2. Classify exception → error_class.
      3. Check (sender, error_class) dedupe → if fired in last 5 min, skip
         the email.
      4. Otherwise, send the alert email via graph.send_mail.
      5. Always send the hand-coded fallback iMessage (per-message, not
         per-window) — UNLESS dedupe blocked the email AND the iMessage
         step would also be redundant. Per spec: fallback iMessage fires
         on every message; only the email is debounced.

    A failure inside steps 4 or 5 is logged and swallowed. The other path
    still runs.
    """
    result = {"email_sent": False, "fallback_imessage_sent": False, "skipped_reason": None}
    try:
        # Step 1 — sender allowlist defense in depth.
        from kavi_runtime.runtime import outbound_scanner
        if not outbound_scanner.is_household_handle(sender_handle):
            result["skipped_reason"] = "non_household_sender"
            return result

        error_class = classify_exception(exc)
        last_error = f"{type(exc).__name__}: {exc}"

        state_path = Path(config["paths"]["imessage_state"])

        # Step 3 — dedupe check, only governs the email.
        should_email = _should_fire_per_sender_alert(
            state_path, sender_handle or "", error_class,
        )

        if should_email:
            subject, body = compose_sender_alert_email(
                sender_handle=sender_handle or "(unknown)",
                error_class=error_class,
                retries_attempted=retries_attempted,
                last_error=last_error,
            )
            try:
                _send_alert_email(config, subject, body)
                result["email_sent"] = True
                logger.warning(
                    "handler_alerts: sent alert email sender=%s error_class=%s",
                    sender_handle, error_class,
                )
                try:
                    from kavi_runtime.structured_log import log_event
                    log_event(
                        "alert", "sender_alert_email_sent",
                        sender=sender_handle, error_class=error_class,
                        debounce=False,
                    )
                except Exception:
                    logger.debug("structured_log sender_alert emit failed (continuing)")
            except Exception as e:
                logger.exception("handler_alerts: alert email send failed: %s", e)
        else:
            result["skipped_reason"] = "email_debounced"
            logger.info(
                "handler_alerts: alert email debounced sender=%s error_class=%s",
                sender_handle, error_class,
            )
            try:
                from kavi_runtime.structured_log import log_event
                log_event(
                    "alert", "sender_alert_email_sent",
                    sender=sender_handle, error_class=error_class,
                    debounce=True,
                )
            except Exception:
                logger.debug("structured_log sender_alert (debounced) emit failed (continuing)")

        # Step 4 — fallback iMessage. Always attempt; per-message, not
        # per-window. If BlueBubbles is also down or the recipient gate
        # rejects, log and continue.
        try:
            sent = _send_fallback_imessage(config, sender_handle or "")
            result["fallback_imessage_sent"] = bool(sent)
            if sent:
                try:
                    from kavi_runtime.structured_log import log_event
                    log_event(
                        "alert", "fallback_imessage_sent",
                        sender=sender_handle,
                    )
                except Exception:
                    logger.debug("structured_log fallback_imessage emit failed (continuing)")
        except Exception as e:
            logger.exception(
                "handler_alerts: fallback iMessage send raised (non-fatal): %s", e,
            )
    except Exception as outer:
        logger.exception("handler_alerts: maybe_alert_failed_handler outer error: %s", outer)
    return result


def maybe_alert_failure_rate(*, config: dict) -> dict[str, Any]:
    """Signal 3 entry point. Called from the wrapped handler after every
    failure. Cheap path when below threshold (just a counter snapshot).
    Returns a dict describing what happened. NEVER raises.

    Threshold: failures/invocations > FAILURE_RATE_THRESHOLD across last
    WINDOW_MINUTES, with a minimum-floor of MIN_INVOCATIONS_BEFORE_FIRING
    to avoid 1/1 = 100% spurious alerts on a brand-new bucket.
    """
    result: dict[str, Any] = {"alerted": False, "stats": None}
    MIN_INVOCATIONS_BEFORE_FIRING = 3
    try:
        stats = current_window_stats()
        result["stats"] = stats
        if stats["invocations"] < MIN_INVOCATIONS_BEFORE_FIRING:
            return result
        if stats["rate"] <= FAILURE_RATE_THRESHOLD:
            return result

        state_path = Path(config["paths"]["imessage_state"])
        if not _should_fire_rate_alert(state_path):
            result["skipped_reason"] = "rate_alert_debounced"
            return result

        subject, body = compose_rate_alert_email(stats)
        try:
            _send_alert_email(config, subject, body)
            result["alerted"] = True
            logger.warning(
                "handler_alerts: sent rate-alert email failures=%d/%d (%.0f%%)",
                stats["failures"], stats["invocations"], stats["rate"] * 100,
            )
            try:
                from kavi_runtime.structured_log import log_event
                log_event(
                    "alert", "rate_threshold_alert_sent",
                    failure_pct=float(stats["rate"]),
                    window_min=int(WINDOW_MINUTES),
                    failures=int(stats["failures"]),
                    invocations=int(stats["invocations"]),
                )
            except Exception:
                logger.debug("structured_log rate_alert emit failed (continuing)")
        except Exception as e:
            logger.exception("handler_alerts: rate-alert email send failed: %s", e)
    except Exception as outer:
        logger.exception("handler_alerts: maybe_alert_failure_rate outer error: %s", outer)
    return result


# ---- last-inbound-seen accessor --------------------------------------------
# The invocation-floor iMessage alarm was removed 2026-05-26 (see the
# constants block above for the full rationale). The `_LAST_INVOCATION_AT`
# dict is still populated on every handler invocation because Signal 3
# (rolling failure-rate alert) uses it; this read accessor stays so the
# `/status` HTTP endpoint can surface "last inbound seen" on demand.


def last_inbound_at() -> float | None:
    """Most recent inbound handler invocation across all handlers, or None
    if no handler has been invoked since the runtime started. Read-only
    accessor for the `/status` page."""
    with _RATE_LOCK:
        if not _LAST_INVOCATION_AT:
            return None
        return max(_LAST_INVOCATION_AT.values())


# ---- low-level send helpers ------------------------------------------------


def _send_alert_email(config: dict, subject: str, body: str) -> None:
    """Send `subject`/`body` to Megha's outlook from her own outlook account
    via graph.send_mail. Reuses the existing helper added 2026-04-29 for the
    Outlook silent-send fallback path.

    Passes bypass_scanner=True (Issue 2 of 2026-05-07 plain-language alert
    rewrite). The outbound content scanner exists to prevent leaks of
    sensitive content to outside parties; alert emails are runtime-to-self
    (Megha's account → Megha herself) and the bodies routinely contain
    Python error messages with digit sequences that false-match the
    generic account-number regex. Without bypass, the operator never gets
    the heads-up email at all.
    """
    own = config.get("imessage", {}).get("own_email_addresses", [])
    megha_outlook = own[0] if own else _household.primary_email("megha")
    # Lazy import — avoids circular import on module load (handlers imports
    # handler_alerts; handler_alerts can't import handlers at the top level).
    from kavi_runtime.handlers import _get_clients
    graph, _, _ = _get_clients(config)
    graph.send_mail(megha_outlook, subject, body, bypass_scanner=True)


_FALLBACK_IMESSAGE_TEXT = (
    "Got your message but I'm degraded right now and couldn't compose a real "
    "reply. Try again in a few minutes. (Auto-fallback, not a real Kavi response.)"
)


def _send_fallback_imessage(config: dict, sender_handle: str) -> bool:
    """Hand-coded fallback iMessage to the original sender. Bypasses the
    persona composer (no LLM) but still flows through the canonical send
    wrapper so the recipient allowlist + content scanner gates run on every
    fallback. Returns True when the underlying send returned sent=True
    (verified or not — the verify step is BlueBubbles' responsibility)."""
    from kavi_runtime.handlers import send_imessage_raw
    res = send_imessage_raw(config, _FALLBACK_IMESSAGE_TEXT, recipient_handle=sender_handle)
    return bool(res.get("sent", False))


# ---- decorator for handler wrapping ----------------------------------------


def _resolve_imessage_sender(payload: dict[str, Any]) -> str | None:
    """Pull the sender handle out of a BlueBubbles webhook payload. Mirror
    of the resolution in handlers.imessage_received so we stay consistent
    on the dedupe key shape."""
    data = payload.get("data") or {}
    handle = data.get("handle")
    if isinstance(handle, dict):
        addr = handle.get("address")
        if addr:
            return addr
    chats = data.get("chats")
    if isinstance(chats, list) and chats:
        guid = chats[0].get("guid") if isinstance(chats[0], dict) else None
        if guid:
            return guid
    return None


def wrap_imessage_handler(handler_fn):
    """Decorator: wrap `imessage_received` so that exceptions trigger
    Signals 1 + 2 + 3 before being re-raised to the caller.

    We re-raise so the existing logging + 202-response flow in server.py
    is unchanged for ops; the alert is purely additive.
    """
    def wrapped(payload: dict[str, Any], config: dict, *args, **kwargs):
        record_invocation("imessage_received")
        try:
            res = handler_fn(payload, config, *args, **kwargs)
        except BaseException as exc:
            error_class = classify_exception(exc)
            record_failure("imessage_received", error_class)
            sender = _resolve_imessage_sender(payload)
            try:
                maybe_alert_failed_handler(
                    config=config,
                    sender_handle=sender,
                    exc=exc,
                    retries_attempted=_anthropic_retries_from_config(config),
                )
            except Exception:
                logger.exception("handler_alerts: failed-handler alert path raised")
            try:
                maybe_alert_failure_rate(config=config)
            except Exception:
                logger.exception("handler_alerts: rate-alert path raised")
            raise
        record_success("imessage_received")
        return res
    return wrapped


def wrap_email_handler(handler_fn):
    """Decorator: wrap `email_arrived`. Same shape as wrap_imessage_handler
    but only fires Signal 3 (no per-sender email alert — emails arrive from
    arbitrary external senders, not household members, so the
    "Megha-sent-iMessage-and-got-no-reply" pattern doesn't apply)."""
    def wrapped(notification, config, account=None, *args, **kwargs):
        record_invocation("email_arrived")
        try:
            res = handler_fn(notification, config, account, *args, **kwargs)
        except BaseException as exc:
            error_class = classify_exception(exc)
            record_failure("email_arrived", error_class)
            try:
                maybe_alert_failure_rate(config=config)
            except Exception:
                logger.exception("handler_alerts: rate-alert path raised")
            raise
        record_success("email_arrived")
        return res
    return wrapped


def _anthropic_retries_from_config(config: dict) -> int:
    """Best-effort: surface the Anthropic SDK's retry budget in the alert
    email so Megha sees how many attempts already burned. The SDK default
    is 2 (3 attempts total) and isn't currently overridden in the runtime;
    if config grows a knob for this later, this helper is the single
    read-site to update.
    """
    return int(config.get("claude", {}).get("max_retries", 2)) + 1
