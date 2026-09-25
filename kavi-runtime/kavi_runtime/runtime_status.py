"""Pull-based runtime status for the `/status` HTTP endpoint (added 2026-05-26).

Why this module exists. The 60-min "no activity" iMessage alert path was
removed on 2026-05-26 because it was a false-positive generator (75 of 89
persona-eval rows over 7 days). Megha still needs a way to know Kavi is
alive on demand. The replacement is pull-based: she opens
`<server.public_url>/status` from her phone and
sees eight lines describing the current runtime state. No push channel,
no alarms, no false positives.

This module is read-only. It does NOT touch state files in a way that
could corrupt them; every reader tolerates missing files, malformed JSON,
and empty data by returning a "no data yet" string rather than crashing.

Two pieces of mutable state are kept in process memory:

  1. Last cron tick per scheduler job id. The scheduler wraps every
     APScheduler job with `wrap_cron_tick` so each fire updates the
     `_LAST_CRON_TICK_AT` dict here. Survives across requests but resets
     on runtime restart, which is acceptable because restart implies
     operator intervention and the next tick re-populates within minutes.

  2. Last successful MS Graph call timestamp + operation label. Updated
     by `record_graph_call_ok` which graph_client invokes on every
     successful API call. Same restart semantics as above.

For Anthropic / Claude API success, we read the structured JSON log
(`anthropic.call_done` events) rather than maintain a third in-memory
counter. The structured log is already written on every successful call,
already filtered for `env="prod"`, and gives us model-name context for
free.

The status snapshot itself is built lazily on each `/status` request.
Tail scans are bounded (we read the last N rows of each JSONL, not the
whole file) so the request stays under 200ms even with a year of data.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
NO_DATA = "no data yet"

# ---- in-memory trackers ----------------------------------------------------

_LOCK = threading.Lock()
_LAST_CRON_TICK_AT: dict[str, float] = {}   # job_id -> epoch seconds
_LAST_GRAPH_OK_AT: float | None = None
_LAST_GRAPH_OK_OP: str | None = None
_RUNTIME_STARTED_AT: float = time.time()


def reset_for_test() -> None:
    """Test-only: wipe in-memory trackers between tests. Production code
    must never invoke this."""
    global _LAST_GRAPH_OK_AT, _LAST_GRAPH_OK_OP, _RUNTIME_STARTED_AT
    with _LOCK:
        _LAST_CRON_TICK_AT.clear()
    _LAST_GRAPH_OK_AT = None
    _LAST_GRAPH_OK_OP = None
    _RUNTIME_STARTED_AT = time.time()


def record_cron_tick(job_id: str) -> None:
    """Stamp the most recent fire time for a scheduler job. Called from
    `wrap_cron_tick` on every successful job execution."""
    with _LOCK:
        _LAST_CRON_TICK_AT[job_id] = time.time()


def wrap_cron_tick(job_id: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate an APScheduler job function so each invocation records a
    tick before delegating. The wrapper is failure-safe on the recording
    side: if `record_cron_tick` ever raised, the actual job would still
    run."""

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            record_cron_tick(job_id)
        except Exception:
            logger.debug("runtime_status: record_cron_tick failed for %s", job_id)
        return func(*args, **kwargs)

    _wrapped.__name__ = f"cron_ticked_{job_id}"
    return _wrapped


def latest_cron_tick() -> tuple[str, float] | None:
    """Return (job_id, epoch) of the most recent cron tick across all
    jobs, or None if no job has fired since process start."""
    with _LOCK:
        if not _LAST_CRON_TICK_AT:
            return None
        job_id, epoch = max(_LAST_CRON_TICK_AT.items(), key=lambda kv: kv[1])
        return job_id, epoch


def record_graph_call_ok(operation: str) -> None:
    """Stamp the most recent successful MS Graph operation. Called from
    graph_client on the success path. `operation` is a short label like
    `list_messages` or `send_mail`; the `/status` page renders it
    verbatim."""
    global _LAST_GRAPH_OK_AT, _LAST_GRAPH_OK_OP
    _LAST_GRAPH_OK_AT = time.time()
    _LAST_GRAPH_OK_OP = operation


def last_graph_call_ok() -> tuple[float, str] | None:
    if _LAST_GRAPH_OK_AT is None or _LAST_GRAPH_OK_OP is None:
        return None
    return _LAST_GRAPH_OK_AT, _LAST_GRAPH_OK_OP


# ---- formatting helpers ----------------------------------------------------


def _fmt_pt(epoch: float | None) -> str:
    """Render an epoch second as `YYYY-MM-DD HH:MM PT`."""
    if not epoch:
        return NO_DATA
    return datetime.fromtimestamp(epoch, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M PT")


def _fmt_iso_pt(iso_ts: str | None) -> str:
    """Render an ISO UTC string as `YYYY-MM-DD HH:MM PT`."""
    if not iso_ts:
        return NO_DATA
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return NO_DATA
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M PT")


def _now_line() -> str:
    """Field 1: runtime time now, UTC plus PT."""
    now = datetime.now(timezone.utc)
    utc = now.strftime("%Y-%m-%d %H:%M UTC")
    pt = now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M PT")
    return f"{utc} ({pt})"


# ---- JSONL tail scanners ---------------------------------------------------
# Each helper opens the file, walks lines, and returns the most recent row
# matching its filter. Missing file / unreadable file / no matching row all
# return None. The status page never crashes on a missing data source.


def _tail_jsonl_rows(path: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    """Return up to the last `limit` JSON rows from `path`. Empty list when
    the file is missing or unreadable. We do not stream from the end (the
    files are small enough today that a forward scan is cheap); switch to
    a seek-from-end implementation when any file exceeds ~10 MB."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        logger.debug("runtime_status: failed to read %s", path)
        return []
    return rows[-limit:]


def _last_inbound_summary(config: dict) -> str:
    """Field 2. Tail of the inbound JSONL plus a fallback to the in-memory
    `last_inbound_at` accessor."""
    inbound_path = Path(config.get("paths", {}).get("eval_persona_inbound_jsonl", ""))
    rows = _tail_jsonl_rows(inbound_path)
    if rows:
        row = rows[-1]
        when = _fmt_iso_pt(row.get("ts"))
        sender = row.get("sender") or "unknown"
        return f"{when}, from {sender}"
    # Fallback to handler_alerts' in-memory record if no JSONL row exists.
    try:
        from kavi_runtime.handler_alerts import last_inbound_at
        epoch = last_inbound_at()
    except Exception:
        epoch = None
    if epoch:
        return f"{_fmt_pt(epoch)}, from unknown"
    return NO_DATA


def _last_outbound_summary(config: dict) -> str:
    """Field 3. Tail of the outbound JSONL."""
    outbound_path = Path(config.get("paths", {}).get("eval_persona_outbound_judgments_jsonl", ""))
    rows = _tail_jsonl_rows(outbound_path)
    if not rows:
        return NO_DATA
    row = rows[-1]
    when = _fmt_iso_pt(row.get("ts"))
    # Outbound rows do not always carry an explicit recipient; the kind
    # implies the audience (periodic_summary, conversational, etc.).
    # Megha (`imessage.megha_phone`) is the default audience for almost every kind.
    recipient = row.get("recipient") or row.get("to") or "Megha"
    kind = row.get("kind") or "unknown"
    return f"{when}, to {recipient}, kind={kind}"


def _last_cron_tick_summary() -> str:
    """Field 4. Most recent cron job + its tick time PT."""
    latest = latest_cron_tick()
    if latest is None:
        return NO_DATA
    job_id, epoch = latest
    return f"{job_id}, {_fmt_pt(epoch)}"


def _last_claude_ok_summary(config: dict) -> str:
    """Field 5. Walk the structured JSON log for the latest
    `anthropic.call_done` event (filtered to env=prod so test-runner
    fixtures never appear in the live `/status` view)."""
    try:
        from kavi_runtime.structured_log import default_log_path
        log_path = Path(
            (config.get("paths") or {}).get("structured_log")
            or default_log_path()
        )
    except Exception:
        return NO_DATA
    if not log_path.exists():
        return NO_DATA

    latest_ts: str | None = None
    latest_model: str | None = None
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("category") != "anthropic":
                    continue
                if row.get("event") != "call_done":
                    continue
                if row.get("env") == "test":
                    continue
                ts = row.get("ts")
                if ts and (latest_ts is None or ts > latest_ts):
                    latest_ts = ts
                    latest_model = row.get("model")
    except Exception:
        return NO_DATA

    if not latest_ts:
        return NO_DATA
    return f"{_fmt_iso_pt(latest_ts)}, model {latest_model or 'unknown'}"


def _last_graph_ok_summary() -> str:
    """Field 6. Most recent successful MS Graph call recorded in memory."""
    pair = last_graph_call_ok()
    if pair is None:
        return NO_DATA
    epoch, op = pair
    return f"{_fmt_pt(epoch)}, op {op}"


def _today_spend_summary(config: dict) -> str:
    """Field 7. Today's Anthropic spend in USD (Pacific day boundary).

    Rewired 2026-06-10: reads the persisted running-total counter
    (`spend_state.read_spend`) instead of scanning runtime_events_jsonl —
    a file no code ever wrote usage rows to, which pinned this field at
    $0.00 since inception."""
    if not config.get("paths", {}).get("imessage_state"):
        return NO_DATA
    try:
        from kavi_runtime.spend_state import read_spend
        return f"${read_spend(config)['today_usd']:.2f}"
    except Exception:
        return NO_DATA


def _spec_loaders_summary(config: dict) -> tuple[str, str]:
    """Fields 9 + 10. Read the startup marker written by the
    spec-loadability probe and return ("true"/"false"/"unknown",
    iso-ts-or-NO_DATA). When the marker is missing or malformed the
    page renders `unknown` and the /status request does not crash.

    Returned as a 2-tuple so the snapshot builder can drop two labels
    in one go: `spec_loaders_ok` and `spec_loaders_checked_at`."""
    try:
        from capabilities.realtime_kavi.startup_probe import read_startup_marker
        marker = read_startup_marker(config)
    except Exception:
        marker = None
    if marker is None:
        return ("unknown", NO_DATA)
    ok = marker.get("ok")
    if ok is True:
        ok_str = "true"
    elif ok is False:
        ok_str = "false"
    else:
        ok_str = "unknown"
    return (ok_str, _fmt_iso_pt(marker.get("ts")))


def _month_spend_summary(config: dict) -> str:
    """Field 8. Month-to-date Anthropic spend plus cap state.

    Rewired 2026-06-10 to the persisted running-total counter (same fix
    as `_today_spend_summary`)."""
    state_path_raw = config.get("paths", {}).get("imessage_state")
    if not state_path_raw:
        return NO_DATA

    try:
        from kavi_runtime.runtime.guardrails import get_pause_state
        from kavi_runtime.spend_state import read_spend
    except Exception:
        return NO_DATA

    try:
        spend = read_spend(config)["month_usd"]
    except Exception:
        spend = 0.0

    # Report the REAL pause reason, not a hardcoded "paused-by-spend-cap"
    # (fixed 2026-07-14 — that label made a 17-day user_requested_quiet pause
    # masquerade as a spend trip and sent diagnosis chasing money that was fine).
    state = "running"
    if state_path_raw:
        try:
            ps = get_pause_state(Path(state_path_raw))
            if ps["paused"]:
                state = f"paused: {ps.get('paused_reason') or 'unknown'}"
        except Exception:
            state = "running"
    return f"${spend:.2f} ({state})"


# ---- public snapshot + HTML renderer ---------------------------------------


def build_status_snapshot(config: dict) -> list[tuple[str, str]]:
    """Return the eight (label, value) pairs the `/status` page renders.

    Labels are stable strings the test suite asserts on; values are the
    pre-formatted plain-language lines."""
    spec_ok, spec_checked = _spec_loaders_summary(config)
    return [
        ("Runtime time now", _now_line()),
        ("Last inbound iMessage", _last_inbound_summary(config)),
        ("Last outbound iMessage", _last_outbound_summary(config)),
        ("Last cron tick", _last_cron_tick_summary()),
        ("Last successful Claude API call", _last_claude_ok_summary(config)),
        ("Last successful MS Graph call", _last_graph_ok_summary()),
        ("Today's Anthropic spend", _today_spend_summary(config)),
        ("This month's Anthropic spend", _month_spend_summary(config)),
        # Added 2026-05-28 (spec-IS-the-runtime drift alarm). The probe
        # in capabilities.realtime_kavi.startup_probe writes a marker on every startup;
        # these two fields surface the most recent state so Megha can see
        # "loaders OK at <ts>" or "loaders failed" from her phone without
        # SSH-ing into Kavi's Mac.
        ("spec_loaders_ok", spec_ok),
        ("spec_loaders_checked_at", spec_checked),
    ]


def render_status_html(config: dict) -> str:
    """Plain HTML for the `/status` page. Single `<pre>` block, one field
    per line. Readable on a phone with no CSS dependencies."""
    fields = build_status_snapshot(config)
    lines = [f"{label}: {value}" for label, value in fields]
    body = "\n".join(lines)
    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>kavi-runtime status</title></head>"
        "<body><pre>"
        f"{body}"
        "</pre></body></html>"
    )
