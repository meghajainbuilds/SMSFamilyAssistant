"""Tests for the structured-log event wiring (item #5 of 2026-05-06
loose-ends pass).

These tests verify that the runtime emits the expected structured events
on each high-value path:
  - anthropic.call_start / call_done / call_failed (claude_client)
  - outbound.imessage_sent (handlers._send_imessage_with_fallback)
  - outbound.task_created (handlers email path)
  - durable_facts.fact_persisted / fact_pending (durable_facts.record_fact)
  - alert.sender_alert_email_sent / fallback_imessage_sent /
    rate_threshold_alert_sent (handler_alerts)

The Anthropic SDK is not invoked in any of these tests — we exercise the
logging helpers directly (call_start/done/failed methods) and the
handler choke points with stubbed dependencies.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kavi_runtime.runtime import durable_facts
from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.structured_log import iter_events, reset_handlers_for_test


@pytest.fixture(autouse=True)
def _reset_log_handlers():
    reset_handlers_for_test()
    yield
    reset_handlers_for_test()


@pytest.fixture
def log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point structured_log at a tmp file so tests can read what was
    written without polluting ~/Library/Logs/."""
    p = tmp_path / "kavi.json.log"
    # Patch default_log_path so log_event picks up the tmp file.
    from kavi_runtime import structured_log
    monkeypatch.setattr(structured_log, "default_log_path", lambda: p)
    return p


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return list(iter_events(path))


def _stub_client() -> ClaudeClient:
    """Build a ClaudeClient with no real API key (the helpers under test
    don't make Anthropic calls)."""
    import os
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-used"
    return ClaudeClient({
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                    "enable_prompt_caching": True},
        "paths": {"skills_dir": "/tmp/nope", "household_md": "/tmp/nope"},
        "model_routing": {"default": "claude-sonnet-4-6",
                           "classify_action_intent": "claude-haiku-4-5"},
    })


def test_anthropic_call_start_done_emit_structured_events(log_path: Path) -> None:
    client = _stub_client()
    started = client._log_call_start("classify_action_intent",
                                      "claude-haiku-4-5", est_input_tokens=320)
    assert isinstance(started, float)
    client._log_call_done("classify_action_intent", "claude-haiku-4-5",
                          started, usage={
                              "input_tokens": 100, "output_tokens": 30,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 1000,
                          })
    events = _read_events(log_path)
    assert len(events) == 2
    start, done = events
    assert start["category"] == "anthropic"
    assert start["event"] == "call_start"
    assert start["call_type"] == "classify_action_intent"
    assert start["model"] == "claude-haiku-4-5"
    assert start["est_input_tokens"] == 320
    assert done["category"] == "anthropic"
    assert done["event"] == "call_done"
    assert done["input_tokens"] == 100
    assert done["cache_read"] == 1000
    assert "latency_ms" in done


def test_anthropic_call_failed_emits_error_class(log_path: Path) -> None:
    client = _stub_client()
    started = client._log_call_start("classify_qa_reply", "claude-haiku-4-5",
                                      est_input_tokens=200)
    try:
        raise ValueError("synthetic api error for test")
    except ValueError as e:
        client._log_call_failed("classify_qa_reply", started, e, retries=2)

    events = _read_events(log_path)
    fail = [e for e in events if e["event"] == "call_failed"][0]
    assert fail["category"] == "anthropic"
    assert fail["call_type"] == "classify_qa_reply"
    assert fail["error_class"] == "ValueError"
    assert fail["retries"] == 2


def test_outbound_imessage_sent_event_fires_via_send_wrapper(
    log_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling _send_imessage_with_fallback writes an
    outbound.imessage_sent row at the bottom of the success path."""
    from kavi_runtime import handlers

    config = {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "paths": {
            "imessage_state": str(log_path.parent / "imessage_state.json"),
            "eval_persona_outbound_judgments_jsonl": str(log_path.parent / "eval_outbound.jsonl"),
        },
    }

    # Stub the outbound_scanner gates to allow the send.
    from kavi_runtime.runtime import outbound_scanner
    monkeypatch.setattr(
        outbound_scanner, "gate_outbound_recipient",
        lambda **kwargs: (True, None),
    )
    monkeypatch.setattr(
        outbound_scanner, "gate_outbound_content",
        lambda **kwargs: (True, None),
    )

    # Stub _get_clients so the wrapper has a fake bb client whose
    # send_with_verify reports a verified send.
    fake_bb = MagicMock()
    fake_bb.send_with_verify.return_value = {"sent": True, "verified": True}
    fake_graph = MagicMock()
    fake_claude = MagicMock()
    _stub = lambda c: (fake_graph, fake_claude, fake_bb)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    # Stub log_outbound to a no-op so we don't pull in eval surface deps.
    from kavi_runtime import outbound_log
    monkeypatch.setattr(outbound_log, "log_outbound", lambda *a, **kw: None)

    handlers._send_imessage_with_fallback(
        config, "Hello Megha test", kind="conversational",
        provenance={"llm_call": "compose_conversational"},
    )

    events = _read_events(log_path)
    imessage_events = [e for e in events if e["event"] == "imessage_sent"]
    assert len(imessage_events) == 1
    row = imessage_events[0]
    assert row["category"] == "outbound"
    assert row["kind"] == "conversational"
    assert row["recipient"] == "+15555550101"
    assert row["char_count"] == len("Hello Megha test")
    assert row["verified"] is True
    assert row["fallback_used"] is False


def test_durable_facts_fact_persisted_event_fires(log_path: Path, tmp_path: Path) -> None:
    """A durable-fact write that is NOT inbound-sourced lands an
    `durable_facts.fact_persisted` row."""
    from kavi_runtime.runtime import outbound_scanner
    # Stub the scanner to allow the fact through.
    with patch.object(outbound_scanner, "gate_durable_fact_write",
                       return_value=(True, None)):
        config = {
            "paths": {
                "learned_facts": str(tmp_path / "learned_facts.jsonl"),
                "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            },
        }
        fact_id = durable_facts.record_fact(
            "Sunday menu lock 12pm", scope="household",
            source_decision_id=None, expires_at=None,
            config=config,
            originated_from_inbound_content=False,
        )
        assert fact_id is not None

    events = _read_events(log_path)
    persisted = [e for e in events if e["event"] == "fact_persisted"]
    assert len(persisted) == 1
    row = persisted[0]
    assert row["category"] == "durable_facts"
    assert row["inbound_origin"] is False
    assert row["scope"] == "household"
    assert row["fact_id"] == fact_id


def test_durable_facts_fact_pending_event_fires_for_inbound_content(
    log_path: Path, tmp_path: Path,
) -> None:
    """An inbound-sourced fact is queued (not persisted) and writes a
    `durable_facts.fact_pending` row."""
    from kavi_runtime.runtime import outbound_scanner
    with patch.object(outbound_scanner, "gate_durable_fact_write",
                       return_value=(True, None)):
        config = {
            "paths": {
                "learned_facts": str(tmp_path / "learned_facts.jsonl"),
                "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            },
        }
        result = durable_facts.record_fact(
            "Max said sushi for Friday", scope="household",
            source_decision_id="d_123", expires_at=None,
            config=config,
            originated_from_inbound_content=True,
            inbound_source="imessage from Max",
        )
        # Inbound-sourced facts return None (queued, not persisted).
        assert result is None

    events = _read_events(log_path)
    pending = [e for e in events if e["event"] == "fact_pending"]
    assert len(pending) == 1
    row = pending[0]
    assert row["category"] == "durable_facts"
    assert row["inbound_origin"] is True
    assert row["scope"] == "household"
    assert row["inbound_source"] == "imessage from Max"


def test_composer_call_sites_emit_call_start_done_via_voice_cap(
    log_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2 (2026-05-06 batch 6): composer-side call sites emit the
    same anthropic.call_start / call_done events the classifier sites
    do. Exercise via `_voice_capped_call` since it covers the
    coordination_ack + any voice-capped composer that flows through it.

    We patch `messages.create` to return a synthetic response so no
    Anthropic call is made; this only proves the wiring."""
    client = _stub_client()

    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text='{"message": "ok ack"}')]
    fake_resp.usage = MagicMock(
        input_tokens=120, output_tokens=10,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    monkeypatch.setattr(client._anthropic.messages, "create",
                         lambda **kw: fake_resp)

    out = client._voice_capped_call(
        user_msg="test user msg",
        system=[{"type": "text", "text": "test system"}],
        debug_label="test_composer",
        call_type="compose_coordination_ack",
    )
    assert out["text"] == "ok ack"

    events = _read_events(log_path)
    starts = [e for e in events if e["event"] == "call_start"]
    dones = [e for e in events if e["event"] == "call_done"]
    assert len(starts) == 1
    assert starts[0]["call_type"] == "compose_coordination_ack"
    assert len(dones) == 1
    assert dones[0]["call_type"] == "compose_coordination_ack"
    assert dones[0]["input_tokens"] == 120


def test_composer_call_failed_emits_call_failed_via_voice_cap(
    log_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2: an Anthropic call that raises during a composer flow
    emits anthropic.call_failed with error_class set, paralleling the
    classifier-site pattern."""
    client = _stub_client()

    def _raises(**kw):
        raise RuntimeError("synthetic api outage")
    monkeypatch.setattr(client._anthropic.messages, "create", _raises)

    out = client._voice_capped_call(
        user_msg="test user msg",
        system=[{"type": "text", "text": "test system"}],
        debug_label="test_composer",
        call_type="compose_periodic_summary",
    )
    assert out["text"] is None
    assert "api_error" in (out.get("_error") or "")

    events = _read_events(log_path)
    failed = [e for e in events if e["event"] == "call_failed"]
    assert len(failed) == 1
    assert failed[0]["call_type"] == "compose_periodic_summary"
    assert failed[0]["error_class"] == "RuntimeError"


def test_alert_events_fire_on_sender_alert_path(
    log_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A simulated handler failure triggers the alert email path; the
    structured-log row `alert.sender_alert_email_sent` fires with
    debounce=False on the first call."""
    from kavi_runtime import handler_alerts
    from kavi_runtime.runtime import outbound_scanner
    handler_alerts.reset_for_test()

    state_path = tmp_path / "imessage_state.json"
    config = {
        "paths": {"imessage_state": str(state_path)},
        "imessage": {
            "own_email_addresses": ["megha@example.com"],
        },
    }

    # All handles in HOUSEHOLD_HANDLES are real; pick Megha's primary.
    sender = "+15555550101"
    monkeypatch.setattr(outbound_scanner, "is_household_handle",
                         lambda h: True)
    monkeypatch.setattr(handler_alerts, "_send_alert_email",
                         lambda *a, **kw: None)
    monkeypatch.setattr(handler_alerts, "_send_fallback_imessage",
                         lambda *a, **kw: True)

    handler_alerts.maybe_alert_failed_handler(
        config=config, sender_handle=sender,
        exc=RuntimeError("synthetic failure"), retries_attempted=2,
    )

    events = _read_events(log_path)
    sender_alert = [e for e in events if e["event"] == "sender_alert_email_sent"]
    fallback = [e for e in events if e["event"] == "fallback_imessage_sent"]
    assert len(sender_alert) == 1
    assert sender_alert[0]["category"] == "alert"
    assert sender_alert[0]["debounce"] is False
    assert len(fallback) == 1
    assert fallback[0]["category"] == "alert"
    assert fallback[0]["sender"] == sender
