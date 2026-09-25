"""Time-boxed quiet window + auto-resume + stuck-pause liveness.

Regression suite for the 2026-06-27 stuck-pause outage: a "go quiet until
tomorrow morning" set auto_runs_paused=true with no end time and only an
inbound "resume" could clear it, so the email->task path was silently gated
for 17 days (912 emails queued, zero tasks). The fix time-boxes every quiet
window, auto-resumes when it elapses, alerts if a quiet pause gets stuck, and
reports the real pause reason on /status.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_quiet_window.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime.state import LOCAL_TZ
from kavi_runtime.runtime.guardrails import (
    clear_paused,
    compute_pause_until,
    get_pause_state,
    quiet_pause_stuck_reason,
    set_paused,
    should_auto_resume,
    _parse_utc_iso,
)


def _state_path(tmp_path: Path) -> Path:
    sp = tmp_path / "state" / "imessage-state.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    return sp


def _cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "imessage_state": str(_state_path(tmp_path)),
            "runtime_events_jsonl": str(tmp_path / "runtime_events.jsonl"),
        },
        "guardrails": {"monthly_anthropic_spend_usd_cap": 30},
        "schedule": {"quiet_hours_start": "21:00", "quiet_hours_end": "07:00"},
    }


# ---- compute_pause_until ----------------------------------------------------


def test_explicit_hours_are_honored() -> None:
    now = datetime(2026, 7, 14, 10, 0, tzinfo=LOCAL_TZ)
    until = _parse_utc_iso(compute_pause_until("go quiet for 2 hours", now))
    assert until == datetime(2026, 7, 14, 12, 0, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def test_an_hour_phrase() -> None:
    now = datetime(2026, 7, 14, 10, 0, tzinfo=LOCAL_TZ)
    until = _parse_utc_iso(compute_pause_until("be quiet for an hour", now))
    assert until == datetime(2026, 7, 14, 11, 0, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def test_minutes_and_half_hour() -> None:
    now = datetime(2026, 7, 14, 10, 0, tzinfo=LOCAL_TZ)
    assert _parse_utc_iso(compute_pause_until("quiet 30 minutes", now)) == \
        datetime(2026, 7, 14, 10, 30, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    assert _parse_utc_iso(compute_pause_until("hush for half an hour", now)) == \
        datetime(2026, 7, 14, 10, 30, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def test_default_resolves_to_next_morning() -> None:
    # No parseable duration at 10 AM -> next 7 AM (tomorrow, since 7 AM today passed).
    now = datetime(2026, 7, 14, 10, 0, tzinfo=LOCAL_TZ)
    until = _parse_utc_iso(compute_pause_until("go quiet", now))
    assert until == datetime(2026, 7, 15, 7, 0, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def test_default_before_morning_resolves_same_day() -> None:
    now = datetime(2026, 7, 14, 5, 0, tzinfo=LOCAL_TZ)
    until = _parse_utc_iso(compute_pause_until("quiet until morning", now))
    assert until == datetime(2026, 7, 14, 7, 0, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def test_hard_24h_cap_on_multiday_request() -> None:
    # "until Monday" has no sub-day duration -> next morning, clamped by 24h.
    now = datetime(2026, 7, 14, 10, 0, tzinfo=LOCAL_TZ)
    until = _parse_utc_iso(compute_pause_until("quiet until Monday", now))
    assert until <= (now + timedelta(hours=24)).astimezone(timezone.utc)


def test_explicit_overlong_duration_is_capped() -> None:
    now = datetime(2026, 7, 14, 10, 0, tzinfo=LOCAL_TZ)
    until = _parse_utc_iso(compute_pause_until("go quiet for 72 hours", now))
    assert until == (now + timedelta(hours=24)).astimezone(timezone.utc)


# ---- set_paused / clear_paused round trip ----------------------------------


def test_quiet_pause_persists_until_and_clears(tmp_path) -> None:
    sp = _state_path(tmp_path)
    pu = compute_pause_until("for 1 hour", datetime(2026, 7, 14, 10, 0, tzinfo=LOCAL_TZ))
    set_paused(sp, "user_requested_quiet", paused_until=pu)
    ps = get_pause_state(sp)
    assert ps["paused"] is True
    assert ps["paused_reason"] == "user_requested_quiet"
    assert ps["paused_until"] == pu

    clear_paused(sp, set_spend_bypass=False)
    ps2 = get_pause_state(sp)
    assert ps2["paused"] is False
    assert ps2["paused_until"] is None


def test_spend_pause_has_no_until(tmp_path) -> None:
    sp = _state_path(tmp_path)
    set_paused(sp, "spend_cap_exceeded", spend_at_trip=31.0)
    assert get_pause_state(sp)["paused_until"] is None


# ---- should_auto_resume -----------------------------------------------------


def test_auto_resume_true_when_window_elapsed(tmp_path) -> None:
    sp = _state_path(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_paused(sp, "user_requested_quiet", paused_until=past)
    assert should_auto_resume(sp) is True


def test_auto_resume_false_when_window_future(tmp_path) -> None:
    sp = _state_path(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_paused(sp, "user_requested_quiet", paused_until=future)
    assert should_auto_resume(sp) is False


def test_auto_resume_never_touches_spend_pause(tmp_path) -> None:
    sp = _state_path(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Even with an elapsed until, a spend pause must NOT auto-resume.
    set_paused(sp, "spend_cap_exceeded", paused_until=past)
    assert should_auto_resume(sp) is False


def test_auto_resume_false_without_until(tmp_path) -> None:
    sp = _state_path(tmp_path)
    set_paused(sp, "user_requested_quiet")  # legacy: no until
    assert should_auto_resume(sp) is False


# ---- quiet_pause_stuck_reason ----------------------------------------------


def test_stuck_when_no_until(tmp_path) -> None:
    sp = _state_path(tmp_path)
    set_paused(sp, "user_requested_quiet")  # un-expirable
    assert quiet_pause_stuck_reason(sp) is not None


def test_stuck_when_overdue_past_grace(tmp_path) -> None:
    sp = _state_path(tmp_path)
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_paused(sp, "user_requested_quiet", paused_until=long_ago)
    assert quiet_pause_stuck_reason(sp) is not None


def test_not_stuck_when_within_window(tmp_path) -> None:
    sp = _state_path(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_paused(sp, "user_requested_quiet", paused_until=future)
    assert quiet_pause_stuck_reason(sp) is None


def test_not_stuck_just_after_elapse(tmp_path) -> None:
    # Elapsed but within grace -> auto-resume handles it, not stuck.
    sp = _state_path(tmp_path)
    just_past = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_paused(sp, "user_requested_quiet", paused_until=just_past)
    assert quiet_pause_stuck_reason(sp) is None


# ---- watchdog ---------------------------------------------------------------


def test_watchdog_auto_resumes_when_due(tmp_path, monkeypatch) -> None:
    from capabilities.realtime_kavi import scheduler
    sp = _state_path(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_paused(sp, "user_requested_quiet", paused_until=past)

    called: dict[str, Any] = {}
    import kavi_runtime.runtime.pause as pause_mod
    monkeypatch.setattr(pause_mod, "_handle_resume",
                        lambda s, c, ir: called.setdefault("resume", ir) or {"status": "resume"})
    scheduler.quiet_window_watchdog(_cfg(tmp_path))
    assert "resume" in called
    assert "auto-resume" in called["resume"]["reason"]


def test_watchdog_alerts_once_when_stuck(tmp_path, monkeypatch) -> None:
    from capabilities.realtime_kavi import scheduler
    sp = _state_path(tmp_path)
    set_paused(sp, "user_requested_quiet")  # no until -> stuck

    sends: list[str] = []
    import kavi_runtime.runtime.alerts as alerts_mod
    monkeypatch.setattr(scheduler, "_send_or_queue_alert",
                        lambda c, s, text, kind, **k: sends.append(kind), raising=False)
    # Patch the symbol the watchdog imports locally.
    monkeypatch.setattr(alerts_mod, "_send_or_queue_alert",
                        lambda c, s, text, kind, **k: sends.append(kind))

    scheduler.quiet_window_watchdog(_cfg(tmp_path))
    scheduler.quiet_window_watchdog(_cfg(tmp_path))  # second call must NOT re-alert
    assert sends == ["stuck_quiet_pause"]
    assert get_pause_state(sp)["paused"] is True  # still paused; alert only


def test_watchdog_ignores_spend_pause(tmp_path, monkeypatch) -> None:
    from capabilities.realtime_kavi import scheduler
    sp = _state_path(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_paused(sp, "spend_cap_exceeded", paused_until=past)
    import kavi_runtime.runtime.pause as pause_mod
    resumed: list[Any] = []
    monkeypatch.setattr(pause_mod, "_handle_resume",
                        lambda s, c, ir: resumed.append(ir))
    scheduler.quiet_window_watchdog(_cfg(tmp_path))
    assert resumed == []  # spend pause left untouched


# ---- resume drain: dedup + spend guard --------------------------------------


def test_resume_drain_dedups_by_message_id(tmp_path, monkeypatch) -> None:
    from kavi_runtime.runtime import pause as pause_mod
    from kavi_runtime.state_per_concept import load_pause_state, save_pause_state
    import kavi_runtime.handlers as h_mod

    sp = _state_path(tmp_path)
    ps = load_pause_state(sp)
    ps["auto_runs_paused"] = True
    ps["paused_reason"] = "user_requested_quiet"
    ps["paused_email_queue"] = [
        {"resourceData": {"id": "AAA"}},
        {"resourceData": {"id": "AAA"}},  # duplicate webhook re-fire
        {"resourceData": {"id": "BBB"}},
    ]
    save_pause_state(sp, ps)

    drained_ids: list[str] = []
    monkeypatch.setattr(
        "capabilities.inbox_to_task.handler.email_arrived",
        lambda notif, config, account=None: drained_ids.append(
            (notif.get("resourceData") or {}).get("id")
        ),
    )
    monkeypatch.setattr(pause_mod, "log_trip", lambda *a, **k: None)
    monkeypatch.setattr(pause_mod, "compute_monthly_spend_usd", lambda c: 0.0)
    monkeypatch.setattr(h_mod, "_get_clients",
                        lambda c: (MagicMock(), MagicMock(), MagicMock()))
    monkeypatch.setattr(h_mod, "_send_imessage_with_fallback",
                        lambda *a, **k: {"verified": True, "fallback_used": False})

    result = pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "test"})
    assert result["duplicates_collapsed"] == 1
    assert drained_ids == ["AAA", "BBB"]  # duplicate never re-classified
    assert get_pause_state(sp)["paused"] is False


def test_resume_drain_stops_and_requeues_at_spend_cap(tmp_path, monkeypatch) -> None:
    from kavi_runtime.runtime import pause as pause_mod
    from kavi_runtime.state_per_concept import load_pause_state, save_pause_state
    import kavi_runtime.handlers as h_mod

    sp = _state_path(tmp_path)
    ps = load_pause_state(sp)
    ps["auto_runs_paused"] = True
    ps["paused_reason"] = "user_requested_quiet"
    ps["paused_email_queue"] = [{"resourceData": {"id": f"M{i}"}} for i in range(3)]
    save_pause_state(sp, ps)

    monkeypatch.setattr("capabilities.inbox_to_task.handler.email_arrived",
                        lambda notif, config, account=None: None)
    monkeypatch.setattr(pause_mod, "log_trip", lambda *a, **k: None)
    # Already over cap at i=0 -> drain nothing, re-pause on spend, re-queue all.
    monkeypatch.setattr(pause_mod, "compute_monthly_spend_usd", lambda c: 99.0)
    monkeypatch.setattr(h_mod, "_get_clients",
                        lambda c: (MagicMock(), MagicMock(), MagicMock()))
    monkeypatch.setattr(h_mod, "_send_imessage_with_fallback",
                        lambda *a, **k: {"verified": True, "fallback_used": False})

    result = pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "test"})
    assert result["spend_stopped"] is True
    assert result["drained_count"] == 0
    ps2 = get_pause_state(sp)
    assert ps2["paused"] is True
    assert ps2["paused_reason"] == "spend_cap_exceeded"
    assert len(load_pause_state(sp)["paused_email_queue"]) == 3  # remainder re-queued


# ---- status label -----------------------------------------------------------


def test_status_label_reports_real_reason(tmp_path) -> None:
    from kavi_runtime import runtime_status
    sp = _state_path(tmp_path)
    set_paused(sp, "user_requested_quiet",
               paused_until=(datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    line = runtime_status._month_spend_summary(_cfg(tmp_path))
    assert "user_requested_quiet" in line
    assert "paused-by-spend-cap" not in line


# ---- 2026-09-23: resume after spend trip, 7-day window, held accounts -------
#
# Regression for the Aug 11 → Sep 23 outage: "resume" after a spend trip
# re-paused one second later with 0 of 92 emails processed, because the drain's
# spend check ignored the bypass the resume had just set. The pause then sat
# silently for 43 days because only quiet pauses had a liveness alarm.


def _seed_spend_pause(sp: Path, queue: list[dict[str, Any]]) -> None:
    from kavi_runtime.state_per_concept import load_pause_state, save_pause_state
    set_paused(sp, "spend_cap_exceeded", spend_at_trip=30.01)
    ps = load_pause_state(sp)
    ps["paused_email_queue"] = queue
    save_pause_state(sp, ps)


def _patch_resume_deps(monkeypatch, spend: float, seen: list[dict[str, Any]],
                       sends: list[str]) -> None:
    from kavi_runtime.runtime import pause as pause_mod
    import kavi_runtime.handlers as h_mod
    monkeypatch.setattr("capabilities.inbox_to_task.handler.email_arrived",
                        lambda notif, config, account=None: seen.append(notif) or {"status": "ok"})
    monkeypatch.setattr(pause_mod, "log_trip", lambda *a, **k: None)
    monkeypatch.setattr(pause_mod, "compute_monthly_spend_usd", lambda c: spend)
    monkeypatch.setattr(h_mod, "_get_clients", lambda c: (MagicMock(), MagicMock(), MagicMock()))
    monkeypatch.setattr(h_mod, "_send_imessage_with_fallback",
                        lambda c, text, kind=None, **k: sends.append(kind) or {"verified": True})


def test_resume_after_spend_trip_same_month_actually_drains(tmp_path, monkeypatch) -> None:
    sp = _state_path(tmp_path)
    _seed_spend_pause(sp, [{"resourceData": {"id": f"M{i}"}} for i in range(3)])
    seen: list[dict[str, Any]] = []
    sends: list[str] = []
    _patch_resume_deps(monkeypatch, spend=30.79, seen=seen, sends=sends)
    from kavi_runtime.runtime import pause as pause_mod

    result = pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "Resume the runs"})
    assert result["spend_stopped"] is False
    assert result["drained_count"] == 3
    assert "drain_spend_paused" not in sends
    assert "drain_complete" in sends
    assert get_pause_state(sp)["paused"] is False


def test_resume_stale_spend_pause_in_new_month_grants_no_bypass(tmp_path, monkeypatch) -> None:
    from kavi_runtime.runtime.guardrails import is_spend_cap_bypassed
    sp = _state_path(tmp_path)
    _seed_spend_pause(sp, [{"resourceData": {"id": "M0"}}])
    seen: list[dict[str, Any]] = []
    _patch_resume_deps(monkeypatch, spend=7.92, seen=seen, sends=[])
    from kavi_runtime.runtime import pause as pause_mod

    result = pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "resume"})
    assert result["drained_count"] == 1
    assert is_spend_cap_bypassed(sp) is False  # cap stays armed for the month


def test_resume_drain_stamps_window_and_holds_accounts(tmp_path, monkeypatch) -> None:
    from kavi_runtime.state_per_concept import load_pause_state
    sp = _state_path(tmp_path)
    _seed_spend_pause(sp, [
        {"resourceData": {"id": "MEG"}, "_source_account": "megha@example.com"},
        {"resourceData": {"id": "MAX"}, "_source_account": "Max@Example.com"},
    ])
    seen: list[dict[str, Any]] = []
    _patch_resume_deps(monkeypatch, spend=0.0, seen=seen, sends=[])
    from kavi_runtime.runtime import pause as pause_mod
    cfg = _cfg(tmp_path)
    cfg["guardrails"].update({"drain_max_age_days": 7, "drain_hold_accounts": ["max@example.com"]})

    result = pause_mod._handle_resume(sp, cfg, {"reason": "resume"})
    assert [n["resourceData"]["id"] for n in seen] == ["MEG"]
    not_before = datetime.fromisoformat(seen[0]["_drain_not_before"])
    assert timedelta(days=6, hours=23) < datetime.now(timezone.utc) - not_before < timedelta(days=7, hours=1)
    assert result["held_count"] == 1
    held = load_pause_state(sp)["paused_email_queue"]
    assert [n["resourceData"]["id"] for n in held] == ["MAX"]


def test_resume_counts_drain_expired_separately(tmp_path, monkeypatch) -> None:
    sp = _state_path(tmp_path)
    _seed_spend_pause(sp, [{"resourceData": {"id": "OLD"}}])
    _patch_resume_deps(monkeypatch, spend=0.0, seen=[], sends=[])
    monkeypatch.setattr("capabilities.inbox_to_task.handler.email_arrived",
                        lambda n, c, a=None: {"status": "skipped", "reason": "drain_window_expired"})
    from kavi_runtime.runtime import pause as pause_mod

    result = pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "resume"})
    assert result["expired_count"] == 1
    assert result["drained_count"] == 0


def test_watchdog_alerts_once_on_long_spend_pause(tmp_path, monkeypatch) -> None:
    from capabilities.realtime_kavi import scheduler
    from kavi_runtime.state_per_concept import load_pause_state, save_pause_state
    import kavi_runtime.runtime.alerts as alerts_mod
    sp = _state_path(tmp_path)
    set_paused(sp, "spend_cap_exceeded", spend_at_trip=30.01)
    ps = load_pause_state(sp)
    ps["paused_since"] = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_pause_state(sp, ps)

    texts: list[str] = []
    monkeypatch.setattr(alerts_mod, "_send_or_queue_alert",
                        lambda c, s, text, kind, **k: texts.append(text))
    scheduler.quiet_window_watchdog(_cfg(tmp_path))
    scheduler.quiet_window_watchdog(_cfg(tmp_path))  # debounced
    assert len(texts) == 1
    assert "3 days" in texts[0] and "spend cap" in texts[0]
    assert get_pause_state(sp)["paused"] is True  # alert only, never auto-resumes


def test_watchdog_quiet_on_fresh_spend_pause(tmp_path, monkeypatch) -> None:
    from capabilities.realtime_kavi import scheduler
    import kavi_runtime.runtime.alerts as alerts_mod
    sp = _state_path(tmp_path)
    set_paused(sp, "spend_cap_exceeded", spend_at_trip=30.01)
    texts: list[str] = []
    monkeypatch.setattr(alerts_mod, "_send_or_queue_alert",
                        lambda c, s, text, kind, **k: texts.append(text))
    scheduler.quiet_window_watchdog(_cfg(tmp_path))
    assert texts == []


def test_handler_skips_drain_expired_email_before_llm(tmp_path, monkeypatch) -> None:
    import kavi_runtime.handlers as h_mod
    from capabilities.inbox_to_task import handler
    graph, claude = MagicMock(), MagicMock()
    graph.fetch_message.return_value = {"id": "OLD", "receivedDateTime": "2026-08-01T10:00:00Z"}
    monkeypatch.setattr(h_mod, "_get_clients", lambda c: (graph, claude, MagicMock()))
    monkeypatch.setattr(h_mod, "is_paused", lambda sp: False)
    monkeypatch.setattr(h_mod, "_dedup_check", lambda mid: None)
    monkeypatch.setattr(h_mod, "_dedup_record", lambda mid, rec: None)

    notif = {"resourceData": {"id": "OLD"}, "_drain_not_before": "2026-09-16T00:00:00+00:00"}
    result = handler._email_arrived_impl(notif, _cfg(tmp_path), None)
    assert result["reason"] == "drain_window_expired"
    assert claude.method_calls == []  # no LLM spend on stale mail
    graph.inbox_folder_id.assert_not_called()


def test_handler_processes_recent_email_inside_window(tmp_path, monkeypatch) -> None:
    import kavi_runtime.handlers as h_mod
    from capabilities.inbox_to_task import handler
    graph = MagicMock()
    graph.fetch_message.return_value = {"id": "NEW", "receivedDateTime": "2026-09-20T10:00:00Z",
                                        "parentFolderId": "JUNK"}
    graph.inbox_folder_id.return_value = "INBOX"
    monkeypatch.setattr(h_mod, "_get_clients", lambda c: (graph, MagicMock(), MagicMock()))
    monkeypatch.setattr(h_mod, "is_paused", lambda sp: False)
    monkeypatch.setattr(h_mod, "_dedup_check", lambda mid: None)
    monkeypatch.setattr(h_mod, "_dedup_record", lambda mid, rec: None)

    notif = {"resourceData": {"id": "NEW"}, "_drain_not_before": "2026-09-16T00:00:00+00:00"}
    result = handler._email_arrived_impl(notif, _cfg(tmp_path), None)
    # Passed the window check; stopped at the next (folder) gate instead.
    assert result["reason"] == "non_inbox_folder"


# ---- 2026-09-23: crash-safe drain (queue stays on disk until handled) ------


def _seed_running_queue(sp: Path, ids: list[str]) -> None:
    from kavi_runtime.state_per_concept import load_pause_state, save_pause_state
    set_paused(sp, "user_requested_quiet")
    ps = load_pause_state(sp)
    ps["paused_email_queue"] = [{"resourceData": {"id": i}} for i in ids]
    save_pause_state(sp, ps)


def _queue_ids(sp: Path) -> list[str]:
    from kavi_runtime.state_per_concept import load_pause_state
    return [n["resourceData"]["id"] for n in load_pause_state(sp).get("paused_email_queue", [])]


def _stub_email_arrived(monkeypatch, behavior: dict[str, Any], seen: list[str]) -> None:
    def fake(notif, config, account=None):
        mid = notif["resourceData"]["id"]
        seen.append(mid)
        b = behavior.get(mid, "ok")
        if b == "exit":
            raise SystemExit("restart mid-drain")
        if b == "error":
            raise RuntimeError("graph token refresh failed")
        return {"status": "ok"}
    monkeypatch.setattr("capabilities.inbox_to_task.handler.email_arrived", fake)


def test_restart_mid_drain_leaves_unhandled_emails_queued(tmp_path, monkeypatch) -> None:
    """Repro of 2026-09-23: a deploy restart mid-drain lost 3,090 emails because
    the queue was emptied before the first email ran."""
    from kavi_runtime.runtime import pause as pause_mod
    sp = _state_path(tmp_path)
    _seed_running_queue(sp, ["m1", "m2", "m3"])
    _patch_resume_deps(monkeypatch, spend=0.0, seen=[], sends=[])
    _stub_email_arrived(monkeypatch, {"m2": "exit"}, [])
    with pytest.raises(SystemExit):
        pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "resume"})
    assert _queue_ids(sp) == ["m2", "m3"]  # m1 handled; m2, m3 survive the "restart"
    assert not pause_mod.drain_marker_path(sp).exists()


def test_failed_email_stays_queued_then_dead_letters(tmp_path, monkeypatch) -> None:
    from kavi_runtime.runtime import pause as pause_mod
    from kavi_runtime.state_per_concept import load_pause_state
    sp = _state_path(tmp_path)
    _seed_running_queue(sp, ["m1", "m2", "m3"])
    _patch_resume_deps(monkeypatch, spend=0.0, seen=[], sends=[])
    _stub_email_arrived(monkeypatch, {"m2": "error"}, [])
    r = pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "resume"})
    assert r["drain_errors"] == 1 and r["drained_count"] == 2
    assert _queue_ids(sp) == ["m2"]  # failure kept for retry, not dropped
    pause_mod.drain_paused_queue(sp, _cfg(tmp_path))
    r3 = pause_mod.drain_paused_queue(sp, _cfg(tmp_path))
    assert r3["dead_lettered"] == 1
    ps = load_pause_state(sp)
    assert ps["paused_email_queue"] == []
    assert [n["resourceData"]["id"] for n in ps["paused_email_dead_letter"]] == ["m2"]


def test_handled_email_removes_its_webhook_refire_copies(tmp_path, monkeypatch) -> None:
    from kavi_runtime.runtime import pause as pause_mod
    sp = _state_path(tmp_path)
    _seed_running_queue(sp, ["A", "A", "B"])
    _patch_resume_deps(monkeypatch, spend=0.0, seen=[], sends=[])
    seen: list[str] = []
    _stub_email_arrived(monkeypatch, {}, seen)
    pause_mod._handle_resume(sp, _cfg(tmp_path), {"reason": "resume"})
    assert seen == ["A", "B"] and _queue_ids(sp) == []


def test_watchdog_resumes_leftover_queue_after_restart(tmp_path, monkeypatch) -> None:
    from capabilities.realtime_kavi import scheduler
    from kavi_runtime.state_per_concept import load_pause_state, save_pause_state
    sp = _state_path(tmp_path)
    _seed_running_queue(sp, ["m2", "m3"])
    clear_paused(sp, set_spend_bypass=False)  # running, but emails left behind
    _patch_resume_deps(monkeypatch, spend=0.0, seen=[], sends=[])
    seen: list[str] = []
    _stub_email_arrived(monkeypatch, {}, seen)
    scheduler.quiet_window_watchdog(_cfg(tmp_path))
    assert seen == ["m2", "m3"] and _queue_ids(sp) == []


def test_watchdog_leaves_held_only_queue_alone(tmp_path, monkeypatch) -> None:
    from capabilities.realtime_kavi import scheduler
    from kavi_runtime.state_per_concept import load_pause_state, save_pause_state
    sp = _state_path(tmp_path)
    ps = load_pause_state(sp)
    ps["paused_email_queue"] = [{"resourceData": {"id": "MX"}, "_source_account": "max@example.com"}]
    save_pause_state(sp, ps)
    cfg = _cfg(tmp_path)
    cfg["guardrails"]["drain_hold_accounts"] = ["max@example.com"]
    seen: list[str] = []
    _stub_email_arrived(monkeypatch, {}, seen)
    scheduler.quiet_window_watchdog(cfg)
    assert seen == [] and _queue_ids(sp) == ["MX"]


def test_only_one_drain_runs_at_a_time(tmp_path) -> None:
    from kavi_runtime.runtime import pause as pause_mod
    sp = _state_path(tmp_path)
    assert pause_mod._DRAIN_LOCK.acquire(blocking=False)
    try:
        r = pause_mod.drain_paused_queue(sp, _cfg(tmp_path))
        assert r["status"] == "drain_already_running"
    finally:
        pause_mod._DRAIN_LOCK.release()
