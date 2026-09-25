"""Tests for the handler-level alerting module
(`kavi_runtime.handler_alerts`).

Verifies that:
- A household-member iMessage handler raise with a 529-style error fires
  exactly one alert email AND one fallback iMessage.
- A second raise within 5 min from the same sender + same error class
  fires the fallback iMessage but NOT a second email (per-key debounce).
- A raise from a non-household sender fires nothing (defense in depth on
  top of the inbound sender gate).
- The rolling failure-rate counter alerts once when failures/invocations
  > 30% in a 5-min window, then debounces for 30 min.
- A successful invocation does NOT fire any alert.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_handler_alerts.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handler_alerts, handlers


# ---- shared fixtures -------------------------------------------------------


def _config_for_test(tmp_path: Path) -> dict[str, Any]:
    """Minimal config sufficient for the alert path. Writes go to tmp_path
    so dedupe state is isolated per test."""
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "max_phone": "+15555550102",
            "own_email_addresses": ["megha@example.com"],
            "chat_guid_prefix": "any;-;",
        },
        "graph": {
            "mstodo_shared_list_id": "AQMkADAwTEST==",
        },
        "bluebubbles": {
            "base_url": "http://127.0.0.1:1234",
            "inbound_webhook_path": "/imessage",
        },
        "claude": {
            "model": "claude-sonnet-4-6", "max_tokens": 1024,
            "enable_prompt_caching": False,
            "max_retries": 2,
        },
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "inbound.jsonl"),
            "imessage_state": str(tmp_path / "imessage-state.json"),
        },
    }


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    """Clear handler client cache + the rolling counter before each test."""
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)
    handler_alerts.reset_for_test()
    yield
    handler_alerts.reset_for_test()


def _wire_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Replace handlers._get_clients with a stub so no real Graph or
    BlueBubbles call ever leaves the test process."""
    graph = MagicMock()
    claude = MagicMock()
    bb = MagicMock()
    bb.send_with_verify.return_value = {
        "sent": True, "verified": True,
        "temp_guid": "fake-guid", "message_guid": None, "send_response": {},
    }
    _stub_get_clients = lambda cfg: (graph, claude, bb)
    monkeypatch.setattr(handlers, "_get_clients", _stub_get_clients)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_get_clients)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub_get_clients)
    return {"graph": graph, "claude": claude, "bb": bb}


class _FakeOverloadedError(Exception):
    """Stand-in for anthropic.OverloadedError. classify_exception keys on
    the type name string, so a bare class with the right name suffices."""


_FakeOverloadedError.__name__ = "OverloadedError"


# ---- exception classifier -------------------------------------------------


def test_classify_exception_overloaded_by_type_name() -> None:
    """A class named OverloadedError → "Anthropic overloaded"."""
    assert handler_alerts.classify_exception(_FakeOverloadedError("upstream busy")) \
        == handler_alerts.ERROR_CLASS_OVERLOADED


def test_classify_exception_overloaded_by_529_in_message() -> None:
    """A 529 substring in the message → "Anthropic overloaded" even if
    the type isn't OverloadedError."""
    assert handler_alerts.classify_exception(RuntimeError("HTTP 529 body=...")) \
        == handler_alerts.ERROR_CLASS_OVERLOADED


def test_classify_exception_state_corruption() -> None:
    """JSONDecodeError → "state corruption"."""
    try:
        json.loads("not json")
    except json.JSONDecodeError as exc:
        assert handler_alerts.classify_exception(exc) \
            == handler_alerts.ERROR_CLASS_STATE_CORRUPTION


def test_classify_exception_unknown() -> None:
    assert handler_alerts.classify_exception(ValueError("something else")) \
        == handler_alerts.ERROR_CLASS_UNKNOWN


# ---- Signals 1 + 2: household-member iMessage failure ----------------------


def test_household_failure_sends_one_email_and_one_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Megha's phone fails the persona LLM call with a 529. We expect
    exactly one graph.send_mail call (the alert email) AND exactly one
    bb.send_with_verify call (the hand-coded fallback iMessage)."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handler_alerts.maybe_alert_failed_handler(
        config=cfg,
        sender_handle="+15555550101",  # Megha (household)
        exc=_FakeOverloadedError("HTTP 529 overloaded"),
        retries_attempted=3,
    )
    assert result["email_sent"] is True
    assert result["fallback_imessage_sent"] is True

    # Email
    mocks["graph"].send_mail.assert_called_once()
    call = mocks["graph"].send_mail.call_args
    to, subject, body = call.args[0], call.args[1], call.args[2]
    assert to == "megha@example.com"
    # Subject leads with user-visible failure (Issue 1 of 2026-05-07 rewrite).
    assert "couldn't reply" in subject.lower()
    # Body uses the four-section plain-language format.
    assert "WHAT BROKE" in body
    assert "WHAT STILL WORKS" in body
    assert "WHAT THIS AFFECTS" in body
    assert "WHAT TO DO" in body
    # Engineering detail (error class + retry count) preserved at the bottom
    # under the technical-detail divider.
    assert "Anthropic overloaded" in body
    assert "Retries attempted: 3" in body
    # Alert emails MUST bypass the outbound scanner so they are not blocked
    # by digit-pattern false positives in Python error messages.
    assert call.kwargs.get("bypass_scanner") is True

    # Fallback iMessage
    mocks["bb"].send_with_verify.assert_called_once()
    sent_text = mocks["bb"].send_with_verify.call_args.args[0]
    assert "Auto-fallback" in sent_text


def test_second_household_failure_within_5min_sends_fallback_but_no_email(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Per-(sender, error_class) email debounce window is 5 min. A second
    raise within that window must NOT send a duplicate alert email but
    must still send the fallback iMessage (per-message)."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    handler_alerts.maybe_alert_failed_handler(
        config=cfg,
        sender_handle="+15555550101",
        exc=_FakeOverloadedError("HTTP 529 overloaded"),
        retries_attempted=3,
    )
    handler_alerts.maybe_alert_failed_handler(
        config=cfg,
        sender_handle="+15555550101",
        exc=_FakeOverloadedError("HTTP 529 overloaded"),
        retries_attempted=3,
    )

    # Email fired ONCE despite two failures
    assert mocks["graph"].send_mail.call_count == 1
    # Fallback iMessage fired both times (per-message)
    assert mocks["bb"].send_with_verify.call_count == 2


def test_non_household_sender_fires_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Defense in depth: if a non-household sender somehow makes it to the
    alert path, do not send an email or fallback iMessage. The inbound
    sender gate already rejects these in production; this covers the
    "what if wrap order ever changes" case."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handler_alerts.maybe_alert_failed_handler(
        config=cfg,
        sender_handle="+19995550000",  # not in HOUSEHOLD_HANDLES
        exc=_FakeOverloadedError("HTTP 529 overloaded"),
        retries_attempted=3,
    )
    assert result["email_sent"] is False
    assert result["fallback_imessage_sent"] is False
    assert result["skipped_reason"] == "non_household_sender"
    mocks["graph"].send_mail.assert_not_called()
    mocks["bb"].send_with_verify.assert_not_called()


def test_different_error_classes_are_not_deduped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Dedupe key is (sender, error_class). Two different error classes
    in the same window → two emails (different classes warrant independent
    visibility)."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    handler_alerts.maybe_alert_failed_handler(
        config=cfg, sender_handle="+15555550101",
        exc=_FakeOverloadedError("HTTP 529"),
        retries_attempted=3,
    )
    handler_alerts.maybe_alert_failed_handler(
        config=cfg, sender_handle="+15555550101",
        exc=ValueError("something different"),
        retries_attempted=3,
    )
    assert mocks["graph"].send_mail.call_count == 2


# ---- Signal 3: rolling failure-rate alert ---------------------------------


def test_failure_rate_alert_fires_once_when_threshold_crossed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """4 invocations, 3 failures → 75% > 30% threshold → one rate-alert
    email. Subsequent failures within the 30-min debounce don't re-fire."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    # Simulate 4 invocations with 3 failures.
    handler_alerts.record_invocation("imessage_received")
    handler_alerts.record_failure("imessage_received", handler_alerts.ERROR_CLASS_OVERLOADED)
    handler_alerts.record_invocation("imessage_received")
    handler_alerts.record_failure("imessage_received", handler_alerts.ERROR_CLASS_OVERLOADED)
    handler_alerts.record_invocation("imessage_received")
    handler_alerts.record_failure("imessage_received", handler_alerts.ERROR_CLASS_OVERLOADED)
    handler_alerts.record_invocation("imessage_received")
    handler_alerts.record_success("imessage_received")

    result = handler_alerts.maybe_alert_failure_rate(config=cfg)
    assert result["alerted"] is True
    mocks["graph"].send_mail.assert_called_once()
    call = mocks["graph"].send_mail.call_args
    subject = call.args[1]
    body = call.args[2]
    # Subject leads with user-visible failure (Issue 1 of 2026-05-07 rewrite).
    assert "crashing more than usual" in subject.lower()
    assert "75%" in subject
    # Body uses the four-section plain-language format.
    assert "WHAT BROKE" in body
    assert "WHAT STILL WORKS" in body
    assert "WHAT THIS AFFECTS" in body
    assert "WHAT TO DO" in body
    # Alert emails MUST bypass the outbound scanner.
    assert call.kwargs.get("bypass_scanner") is True

    # Second call with no new data → still over threshold but debounced.
    result2 = handler_alerts.maybe_alert_failure_rate(config=cfg)
    assert result2["alerted"] is False
    assert mocks["graph"].send_mail.call_count == 1


def test_failure_rate_alert_does_not_fire_below_minimum_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """1 invocation, 1 failure = 100% but only 1 sample. Don't fire; we
    require a minimum invocation floor to avoid spurious 1/1 alerts on a
    fresh bucket."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    handler_alerts.record_invocation("imessage_received")
    handler_alerts.record_failure("imessage_received", handler_alerts.ERROR_CLASS_OVERLOADED)

    result = handler_alerts.maybe_alert_failure_rate(config=cfg)
    assert result["alerted"] is False
    mocks["graph"].send_mail.assert_not_called()


def test_successful_invocation_fires_no_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No exception → no alert path runs."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    @handler_alerts.wrap_imessage_handler
    def fake_handler(payload, config):
        return {"status": "ok"}

    res = fake_handler(
        {"type": "new-message", "data": {"handle": {"address": "+15555550101"}, "text": "hi"}},
        cfg,
    )
    assert res == {"status": "ok"}
    mocks["graph"].send_mail.assert_not_called()
    mocks["bb"].send_with_verify.assert_not_called()


# ---- decorator integration -------------------------------------------------


def test_wrap_imessage_handler_fires_alert_and_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The decorator catches the exception, runs the alert path, then
    re-raises so server.py's existing logger.exception line is unchanged."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    @handler_alerts.wrap_imessage_handler
    def fake_handler(payload, config):
        raise _FakeOverloadedError("HTTP 529 overloaded")

    payload = {
        "type": "new-message",
        "data": {"handle": {"address": "+15555550101"}, "text": "hello"},
    }
    with pytest.raises(_FakeOverloadedError):
        fake_handler(payload, cfg)

    mocks["graph"].send_mail.assert_called_once()
    mocks["bb"].send_with_verify.assert_called_once()
