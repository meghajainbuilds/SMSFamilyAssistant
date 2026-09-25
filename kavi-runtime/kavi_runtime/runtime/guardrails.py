"""Production safety nets for the always-on runtime (step 12, 2026-04-29).

Three guardrails:
  1. Spend cap — monthly Sonnet spend computed from runs.jsonl usage tokens.
     Trip → pause auto-runs + iMessage Megha. Resume requires intent-classifier yes.
  2. Webhook flood — rolling 60-min count of inbound MS Graph notifications.
     Trip → throttle to 1/min via deferred queue + iMessage Megha. No pause.
  3. Per-call token cap — pre-call estimate before each Sonnet call.
     Trip → halt that call + iMessage Megha with subject. No pause.

Spend tracking (rewired 2026-06-10): reads the persisted running-total
counter at `state/anthropic_spend.json` (see `kavi_runtime/spend_state.py`),
incremented by `ClaudeClient._log_call_done` after every Claude call. The
prior design ("compute on the fly from runs.jsonl, no separate counter to
drift") summed a file no code ever wrote usage rows to — every reading was
$0.00 and the cap was unreachable. Pause flag persists in pause_state.json
so launchd KeepAlive restarts don't lose the safe state.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.pricing import cost_usd_from_usage
from kavi_runtime.state import (
    LOCAL_TZ,
    append_run,
    utc_now_iso,
)
from kavi_runtime.state_per_concept import (
    load_pause_state,
    save_pause_state,
    load_pending_alerts,
    save_pending_alerts,
)

logger = logging.getLogger(__name__)

def compute_call_cost_usd(usage: dict[str, int] | None,
                          model: str = "claude-sonnet-4-6") -> float:
    """Cost in USD for one call, given a usage dict from the API response.

    Back-compat wrapper over the canonical `kavi_runtime.pricing` module
    (2026-06-10 dedupe). Callers that know the model should pass it;
    the default keeps legacy single-arg call sites (backfill script)
    pricing at Sonnet rates as before.
    """
    return cost_usd_from_usage(model, usage)


def _current_month_label() -> str:
    """e.g. '2026-04' — used to scope the spend-cap bypass flag to one calendar month."""
    return datetime.now(LOCAL_TZ).strftime("%Y-%m")


def compute_monthly_spend_usd(config: dict) -> float:
    """Month-to-date Anthropic spend (Pacific calendar month).

    Rewired 2026-06-10: reads the persisted running-total counter
    (`spend_state.read_spend`) instead of summing `runs.jsonl` usage rows —
    a file no code ever wrote usage rows to, which kept this function at
    $0.00 since inception and made the spend-cap trip unreachable.

    Signature change (runs_path → config): the spend-state path derives
    from `paths.imessage_state`; callers already hold config.
    """
    from kavi_runtime.spend_state import read_spend
    return read_spend(config)["month_usd"]


def estimate_input_tokens(*texts: str) -> int:
    """Rough char/4 heuristic. Good enough for a coarse 50K threshold; we don't need
    Anthropic's count_tokens API just to decide whether to halt a runaway call."""
    total_chars = sum(len(t or "") for t in texts)
    return total_chars // 4


# ---------- Time-boxed quiet window (added 2026-07-14 after the 17-day stuck-pause) ----------
#
# A "go quiet" iMessage set auto_runs_paused=true with NO end time and only an
# inbound "resume" could clear it, so a "quiet until tomorrow morning" became a
# permanent silent outage (2026-06-27 → 2026-07-14, 912 emails queued, zero
# tasks). The fix: every user_requested_quiet pause carries a `paused_until`
# (UTC ISO), the scheduler auto-resumes once it elapses, and the window is
# hard-capped at 24h so an ambiguous quiet can never outlive a day.

_QUIET_MAX_HOURS = 24  # hard ceiling: a quiet window can never exceed one day
_MORNING_HOUR_PT = 7   # default wake time (aligns with the 7 AM digest / alert drain)


def _parse_utc_iso(s: str) -> datetime:
    """Parse a utc_now_iso()-style 'YYYY-MM-DDTHH:MM:SSZ' string to an aware UTC datetime."""
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _next_morning_pt(now_pt: datetime) -> datetime:
    """The next 7:00 AM Pacific at or after `now_pt` (strictly after if already past 7 AM today)."""
    target = now_pt.replace(hour=_MORNING_HOUR_PT, minute=0, second=0, microsecond=0)
    if target <= now_pt:
        target = target + timedelta(days=1)
    return target


def compute_pause_until(text: str, now_pt: datetime | None = None) -> str:
    """Resolve a quiet-window end time from a "go quiet" request.

    Deterministic (a computation feeding state, not user-facing text): parse an
    explicit sub-day duration ("for an hour", "30 minutes", "for 2 hours") when
    present, else default to the next 7 AM Pacific ("until tomorrow morning",
    "until Monday", or a bare "go quiet"). Everything is hard-capped at 24h so a
    quiet window can never silently outlive a day. Returns a UTC ISO 'Z' string.
    """
    if now_pt is None:
        now_pt = datetime.now(LOCAL_TZ)
    ceiling = now_pt + timedelta(hours=_QUIET_MAX_HOURS)
    t = (text or "").lower()

    if "half an hour" in t or "half hour" in t:
        until = now_pt + timedelta(minutes=30)
    elif re.search(r"\ban? hour\b", t):
        until = now_pt + timedelta(hours=1)
    elif (m := re.search(r"(\d+)\s*(?:hours?|hrs?|h)\b", t)):
        until = now_pt + timedelta(hours=int(m.group(1)))
    elif (mm := re.search(r"(\d+)\s*(?:minutes?|mins?|m)\b", t)):
        until = now_pt + timedelta(minutes=int(mm.group(1)))
    else:
        # "until tomorrow", "tomorrow morning", "until Monday", bare "go quiet"
        # all resolve to the next morning, clamped by the 24h ceiling.
        until = _next_morning_pt(now_pt)

    if until > ceiling:
        until = ceiling
    return until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def should_auto_resume(state_path: Path, now: datetime | None = None) -> bool:
    """True iff a user_requested_quiet pause's window has elapsed and it is safe
    to auto-resume. Only quiet pauses auto-expire — spend/webhook/api pauses
    require the underlying condition to clear plus Megha's confirm."""
    ps = get_pause_state(state_path)
    if not ps["paused"] or ps.get("paused_reason") != "user_requested_quiet":
        return False
    pu = ps.get("paused_until")
    if not pu:
        return False
    try:
        until_dt = _parse_utc_iso(pu)
    except Exception:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= until_dt


def quiet_pause_stuck_reason(state_path: Path, grace_hours: int = 1,
                             now: datetime | None = None) -> str | None:
    """Return a plain reason string if a user_requested_quiet pause is STUCK
    (should have auto-resumed but hasn't), else None. Catches the backstop's
    own failure modes: a legacy pause with no wake time, an unreadable wake
    time, or the auto-resume job not firing (overdue past a grace window)."""
    ps = get_pause_state(state_path)
    if not ps["paused"] or ps.get("paused_reason") != "user_requested_quiet":
        return None
    pu = ps.get("paused_until")
    now = now or datetime.now(timezone.utc)
    if not pu:
        return "no wake time was ever set"
    try:
        overdue_at = _parse_utc_iso(pu) + timedelta(hours=grace_hours)
    except Exception:
        return "the wake time is unreadable"
    if now >= overdue_at:
        return "the scheduled wake-up did not fire"
    return None


def get_pause_state(state_path: Path) -> dict[str, Any]:
    """Read pause-related fields. Direct per-concept read (Phase 5,
    2026-06-02 — formerly went through the load_imessage_state shim)."""
    ps = load_pause_state(state_path)
    return {
        "paused": bool(ps.get("auto_runs_paused", False)),
        "paused_since": ps.get("paused_since"),
        "paused_reason": ps.get("paused_reason"),
        "paused_until": ps.get("paused_until"),
        "spend_cap_bypass_month": ps.get("spend_cap_bypass_month"),
    }


def is_paused(state_path: Path) -> bool:
    return get_pause_state(state_path)["paused"]


def is_spend_cap_bypassed(state_path: Path) -> bool:
    """True if Megha already resumed the runtime in this calendar month after a spend trip.
    The bypass clears automatically on the 1st of the next month (Pacific)."""
    ps = load_pause_state(state_path)
    return ps.get("spend_cap_bypass_month") == _current_month_label()


def set_paused(state_path: Path, reason: str, spend_at_trip: float | None = None,
               paused_until: str | None = None) -> None:
    """Pause auto-runs. `paused_until` (UTC ISO) is set only for a time-boxed
    user_requested_quiet window; spend/webhook/api pauses leave it None (they
    must NOT auto-resume). Setting a fresh pause clears any prior stuck-pause
    liveness marker so the next stuck episode can alert again."""
    ps = load_pause_state(state_path)
    ps["auto_runs_paused"] = True
    ps["paused_since"] = utc_now_iso()
    ps["paused_reason"] = reason
    ps["paused_until"] = paused_until
    ps["liveness_alert_sent"] = False
    if spend_at_trip is not None:
        ps["paused_spend_usd"] = round(spend_at_trip, 4)
    save_pause_state(state_path, ps)


def clear_paused(state_path: Path, set_spend_bypass: bool) -> dict[str, Any]:
    """Clear pause flag. If set_spend_bypass=True (resume after spend trip), set the
    monthly bypass flag so the very next email doesn't immediately re-trip the cap.
    Returns a SNAPSHOT of the queued paused emails so the caller can drain them.

    The queue itself stays on disk (changed 2026-09-23). It used to be emptied
    here, before a single email was processed, so a restart or crash mid-drain
    silently lost everything not yet handled (3,090 emails on 2026-09-23). The
    drain now removes each email only after it is handled
    (`remove_paused_email`), and a boot/watchdog pass resumes any leftover."""
    ps = load_pause_state(state_path)
    queue = list(ps.get("paused_email_queue", []))
    ps["auto_runs_paused"] = False
    ps["paused_since"] = None
    ps["paused_reason"] = None
    ps["paused_until"] = None
    ps["liveness_alert_sent"] = False
    if set_spend_bypass:
        ps["spend_cap_bypass_month"] = _current_month_label()
    save_pause_state(state_path, ps)
    return {"drained_queue": queue}


def queued_message_id(notification: Any) -> str:
    """Graph message id of a queued notification ("" if absent)."""
    if not isinstance(notification, dict):
        return ""
    rd = notification.get("resourceData") or {}
    return rd.get("id") or notification.get("resource", "").split("/")[-1] or ""


def _same_queued_email(a: Any, b: Any) -> bool:
    mid = queued_message_id(a)
    return queued_message_id(b) == mid if mid else a == b


def remove_paused_email(state_path: Path, notification: Any) -> None:
    """Remove every queued copy of this email (webhook re-fires included). Called
    only after the email has been handled, so a crash leaves it queued."""
    ps = load_pause_state(state_path)
    queue = ps.get("paused_email_queue", [])
    ps["paused_email_queue"] = [n for n in queue if not _same_queued_email(n, notification)]
    save_pause_state(state_path, ps)


DRAIN_MAX_ATTEMPTS = 3


def record_drain_failure(state_path: Path, notification: Any) -> bool:
    """Count a failed drain attempt on the queued email. After
    DRAIN_MAX_ATTEMPTS it moves to `paused_email_dead_letter` (kept for a human
    look, never silently dropped). Returns True if it was dead-lettered."""
    ps = load_pause_state(state_path)
    queue = ps.get("paused_email_queue", [])
    kept: list[Any] = []
    dead = False
    for n in queue:
        if _same_queued_email(n, notification) and isinstance(n, dict):
            n = {**n, "_drain_attempts": int(n.get("_drain_attempts", 0)) + 1}
            if n["_drain_attempts"] >= DRAIN_MAX_ATTEMPTS:
                ps.setdefault("paused_email_dead_letter", []).append(n)
                dead = True
                continue
        kept.append(n)
    ps["paused_email_queue"] = kept
    save_pause_state(state_path, ps)
    return dead


def enqueue_paused_email(state_path: Path, notification: dict[str, Any]) -> None:
    """While paused, every inbound MS Graph notification gets stored verbatim so we
    can re-dispatch on resume. Persisted to disk to survive launchd restarts."""
    ps = load_pause_state(state_path)
    queue = ps.get("paused_email_queue", [])
    queue.append(notification)
    ps["paused_email_queue"] = queue
    save_pause_state(state_path, ps)


def enqueue_pending_alert(state_path: Path, alert: dict[str, Any]) -> None:
    """Quiet-hours queue for guardrail-trip iMessages. Drained at 7 AM as standalone
    messages, NOT bundled into the morning digest."""
    pa = load_pending_alerts(state_path)
    alerts = pa.get("pending_alerts", [])
    alerts.append({**alert, "queued_at": utc_now_iso()})
    pa["pending_alerts"] = alerts
    save_pending_alerts(state_path, pa)


def drain_pending_alerts(state_path: Path) -> list[dict[str, Any]]:
    pa = load_pending_alerts(state_path)
    alerts = pa.get("pending_alerts", [])
    pa["pending_alerts"] = []
    save_pending_alerts(state_path, pa)
    return alerts


# ---------- Webhook flood tracking (in-memory) ----------

_lock = threading.Lock()
_webhook_arrivals: deque[float] = deque()
_deferred_queue: list[dict[str, Any]] = []
_flood_alert_last_sent: float = 0.0  # debounce so we iMessage at most once/hour


def record_webhook_arrival() -> int:
    """Call once per inbound MS Graph notification. Returns the rolling 60-min count
    (after pruning + appending now)."""
    now = time.time()
    cutoff = now - 3600
    with _lock:
        while _webhook_arrivals and _webhook_arrivals[0] < cutoff:
            _webhook_arrivals.popleft()
        _webhook_arrivals.append(now)
        return len(_webhook_arrivals)


def webhook_flood_active(config: dict) -> bool:
    threshold = config["guardrails"]["webhook_flood_per_hour"]
    with _lock:
        now = time.time()
        cutoff = now - 3600
        while _webhook_arrivals and _webhook_arrivals[0] < cutoff:
            _webhook_arrivals.popleft()
        return len(_webhook_arrivals) > threshold


def webhook_count_last_hour() -> int:
    with _lock:
        return len(_webhook_arrivals)


def enqueue_deferred(notification: dict[str, Any]) -> None:
    with _lock:
        _deferred_queue.append(notification)


def pop_deferred() -> dict[str, Any] | None:
    with _lock:
        return _deferred_queue.pop(0) if _deferred_queue else None


def deferred_queue_size() -> int:
    with _lock:
        return len(_deferred_queue)


def should_send_flood_alert() -> bool:
    """Debounce: send a flood-alert iMessage at most once per hour."""
    global _flood_alert_last_sent
    with _lock:
        now = time.time()
        if now - _flood_alert_last_sent < 3600:
            return False
        _flood_alert_last_sent = now
        return True


# ---------- Trip telemetry ----------

def log_trip(runs_path: Path, guardrail: str, threshold: Any, actual: Any, action: str,
             extra: dict[str, Any] | None = None) -> None:
    """Append a guardrail_trip row to runs.jsonl."""
    record = {
        "run_id": utc_now_iso(),
        "event_type": "guardrail_trip",
        "ts": utc_now_iso(),
        "guardrail": guardrail,
        "threshold": threshold,
        "actual": actual,
        "action": action,
        "annotations": None,
    }
    if extra:
        record.update(extra)
    try:
        append_run(runs_path, record)
    except Exception as e:
        logger.warning("guardrail_trip log failed: %s", e)
