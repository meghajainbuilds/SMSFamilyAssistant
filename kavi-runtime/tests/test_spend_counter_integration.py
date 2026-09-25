"""Integration tests for the 2026-06-10 spend-counter fix.

End-to-end seams covered:
  1. ClaudeClient._log_call_done → spend_state (every messages.create site
     flows through _log_call_done, so hooking it covers the whole codebase).
  2. The $30/month cap trip path is REACHABLE: a call that crosses the cap
     pauses auto-runs, writes the guardrail_trip row, and fires the alert.
  3. /status renders non-zero spend from the persisted counter.
  4. error_budget counts the structured-log short field names
     (cache_creation / cache_read) and still accepts the legacy long names.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kavi_runtime import claude_client
from kavi_runtime.runtime import guardrails, spend_cap
from kavi_runtime.spend_state import read_spend, record_spend, spend_state_path


def _client_config(tmp_path: Path) -> dict:
    return {
        "paths": {
            "skills_dir": str(Path(__file__).resolve().parent.parent / "skills"),
            "household_md": str(tmp_path / "household.md"),
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "runtime_events_jsonl": str(tmp_path / "runs.jsonl"),
        },
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 600,
                   "enable_prompt_caching": False},
        "guardrails": {"monthly_anthropic_spend_usd_cap": 30},
    }


def _mock_client(cfg: dict, monkeypatch: pytest.MonkeyPatch,
                 *, usage_kwargs: dict) -> claude_client.ClaudeClient:
    """Real ClaudeClient with the Anthropic SDK instance replaced by a mock
    whose messages.create returns a JSON-shaped response with `usage_kwargs`
    token counts (the test_pending_clarification construction pattern)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cc = claude_client.ClaudeClient(cfg)
    resp = MagicMock()
    resp.content = [MagicMock(text='{"message": "hi"}')]
    resp.usage = MagicMock(**usage_kwargs)
    cc._anthropic = MagicMock()
    cc._anthropic.messages.create.return_value = resp
    return cc


# Sonnet: (1000*3 + 200*15 + 50000*3.75 + 10000*0.30) / 1M = $0.1965
USAGE = dict(input_tokens=1000, output_tokens=200,
             cache_creation_input_tokens=50_000,
             cache_read_input_tokens=10_000)
EXPECTED_CALL_COST = 0.1965


# ---- 1. _log_call_done hooks the spend counter -------------------------------


def test_log_call_done_increments_spend_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _client_config(tmp_path)
    cc = _mock_client(cfg, monkeypatch, usage_kwargs=USAGE)

    out = cc._voice_capped_call(
        "hello", [{"type": "text", "text": "sys"}], "spend_hook_test",
    )
    assert out["text"] == "hi"

    spend = read_spend(cfg)
    assert spend["today_usd"] == pytest.approx(EXPECTED_CALL_COST)
    assert spend["month_usd"] == pytest.approx(EXPECTED_CALL_COST)
    # The state file records the call attribution for debuggability.
    data = json.loads(spend_state_path(cfg).read_text())
    assert data["last_call"]["model"] == "claude-sonnet-4-6"
    assert data["last_call"]["usd"] == pytest.approx(EXPECTED_CALL_COST)


def test_log_call_done_spend_accumulates_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _client_config(tmp_path)
    cc = _mock_client(cfg, monkeypatch, usage_kwargs=USAGE)

    cc._voice_capped_call("a", [{"type": "text", "text": "s"}], "t")
    cc._voice_capped_call("b", [{"type": "text", "text": "s"}], "t")

    assert read_spend(cfg)["month_usd"] == pytest.approx(2 * EXPECTED_CALL_COST)


def test_log_call_done_survives_spend_hook_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken spend counter must never break the API call path."""
    cfg = _client_config(tmp_path)
    cc = _mock_client(cfg, monkeypatch, usage_kwargs=USAGE)

    import kavi_runtime.spend_state as ss

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ss, "record_spend_from_usage", boom)
    out = cc._voice_capped_call("a", [{"type": "text", "text": "s"}], "t")
    assert out["text"] == "hi"  # the call result is unaffected


# ---- 2. THE CAP FIRES ---------------------------------------------------------


def test_cap_does_not_trip_below_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _client_config(tmp_path)
    state_path = Path(cfg["paths"]["imessage_state"])
    alerts: list = []
    monkeypatch.setattr(spend_cap, "_send_or_queue_alert",
                        lambda *a, **k: alerts.append((a, k)))

    record_spend(cfg, 29.90, model="claude-sonnet-4-6", call_type="t")
    spend_cap._check_spend_cap_after_call(cfg, state_path)

    assert not guardrails.is_paused(state_path)
    assert alerts == []
    runs_path = Path(cfg["paths"]["runtime_events_jsonl"])
    assert not runs_path.exists() or "guardrail_trip" not in runs_path.read_text()


def test_cap_trips_when_a_call_crosses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed the counter just under $30, run a (mocked) Claude call whose
    cost crosses the cap, and assert the existing trip machinery executes:
    pause flag set, guardrail_trip row written, alert sent/queued."""
    cfg = _client_config(tmp_path)
    state_path = Path(cfg["paths"]["imessage_state"])
    alerts: list = []
    monkeypatch.setattr(spend_cap, "_send_or_queue_alert",
                        lambda *a, **k: alerts.append((a, k)))

    record_spend(cfg, 29.90, model="claude-sonnet-4-6", call_type="seed")

    # The crossing call goes through the real _log_call_done hook.
    cc = _mock_client(cfg, monkeypatch, usage_kwargs=USAGE)
    cc._voice_capped_call("a", [{"type": "text", "text": "s"}], "t")
    assert read_spend(cfg)["month_usd"] == pytest.approx(29.90 + EXPECTED_CALL_COST)

    spend_cap._check_spend_cap_after_call(cfg, state_path)

    # Pause flag set with the spend recorded at trip time.
    pause = guardrails.get_pause_state(state_path)
    assert pause["paused"] is True
    assert pause["paused_reason"] == "spend_cap_exceeded"

    # guardrail_trip telemetry row written to runs.jsonl.
    rows = [json.loads(line) for line in
            Path(cfg["paths"]["runtime_events_jsonl"]).read_text().splitlines()
            if line.strip()]
    trips = [r for r in rows if r.get("event_type") == "guardrail_trip"]
    assert len(trips) == 1
    assert trips[0]["guardrail"] == "monthly_spend_cap"
    assert trips[0]["threshold"] == 30
    assert trips[0]["actual"] == pytest.approx(29.90 + EXPECTED_CALL_COST, abs=1e-3)

    # Trip alert fired exactly once, naming the cap.
    assert len(alerts) == 1
    alert_text = alerts[0][0][2]
    assert "$30" in alert_text and "spend cap" in alert_text.lower()


def test_cap_does_not_double_trip_when_already_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _client_config(tmp_path)
    state_path = Path(cfg["paths"]["imessage_state"])
    alerts: list = []
    monkeypatch.setattr(spend_cap, "_send_or_queue_alert",
                        lambda *a, **k: alerts.append((a, k)))

    record_spend(cfg, 31.00, model="claude-sonnet-4-6", call_type="t")
    spend_cap._check_spend_cap_after_call(cfg, state_path)
    spend_cap._check_spend_cap_after_call(cfg, state_path)
    assert len(alerts) == 1


# ---- 3. /status renders the counter ------------------------------------------


def test_status_renders_non_zero_spend_from_seeded_state(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from kavi_runtime import runtime_status
    from capabilities.realtime_kavi.server import build_app

    cfg = {
        "bluebubbles": {"inbound_webhook_path": "/bluebubbles/inbound"},
        "imessage": {"megha_phone": "+15555550101"},
        "paths": {
            "eval_inbox_judgments_jsonl": str(tmp_path / "eval-inbox.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "inbound.jsonl"),
            "runtime_events_jsonl": str(tmp_path / "runs.jsonl"),
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "structured_log": str(tmp_path / "kavi-runtime.json.log"),
        },
    }
    record_spend(cfg, 1.23, model="claude-sonnet-4-6", call_type="t")

    runtime_status.reset_for_test()
    client = TestClient(build_app(cfg))
    body = client.get("/status").text

    assert "Today's Anthropic spend: $1.23" in body
    assert "This month's Anthropic spend: $1.23 (running)" in body


def test_status_spend_increases_after_a_hooked_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry verify gate: spend must increase across a (synthetic)
    compose call."""
    cfg = _client_config(tmp_path)
    record_spend(cfg, 0.50, model="claude-sonnet-4-6", call_type="t")
    before = read_spend(cfg)["today_usd"]

    cc = _mock_client(cfg, monkeypatch, usage_kwargs=USAGE)
    cc._voice_capped_call("a", [{"type": "text", "text": "s"}], "t")

    after = read_spend(cfg)["today_usd"]
    assert after > before
    assert after == pytest.approx(0.50 + EXPECTED_CALL_COST)


# ---- 4. error_budget field names ----------------------------------------------


def _rollup_for_event(tmp_path: Path, event_fields: dict) -> dict:
    from kavi_runtime.error_budget import compute_rollup

    now = datetime(2026, 6, 10, 7, 0, tzinfo=timezone.utc)
    log_path = tmp_path / "kavi.json.log"
    log_path.write_text(json.dumps({
        "ts": (now - timedelta(hours=1)).isoformat(),
        "category": "anthropic", "event": "call_done",
        **event_fields,
    }) + "\n")
    return compute_rollup(log_path, now=now, window_hours=24)


def test_error_budget_counts_short_cache_field_names(tmp_path: Path) -> None:
    """The structured-log rows written by _log_call_done use cache_creation /
    cache_read. Before 2026-06-10 these were silently dropped — cache-write
    is the dominant cost component."""
    rollup = _rollup_for_event(tmp_path, {
        "model": "claude-sonnet-4-6",
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation": 100_000, "cache_read": 0,
    })
    assert rollup["anthropic_spend_usd"] == pytest.approx(0.375)  # 100K * $3.75/M


def test_error_budget_still_accepts_legacy_long_cache_field_names(
    tmp_path: Path,
) -> None:
    rollup = _rollup_for_event(tmp_path, {
        "model": "claude-sonnet-4-6",
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 100_000,
        "cache_read_input_tokens": 0,
    })
    assert rollup["anthropic_spend_usd"] == pytest.approx(0.375)


# ---- Raw SDK usage shape (Verifier FAIL 2026-06-10) ---------------------------

RAW_SDK_USAGE = dict(
    input_tokens=251,
    output_tokens=25,
    cache_creation={"ephemeral_1h_input_tokens": 0,
                    "ephemeral_5m_input_tokens": 27_563},
    cache_creation_input_tokens=27_563,
    cache_read_input_tokens=0,
)
# Sonnet: (251*3 + 25*15 + 27563*3.75) / 1M
RAW_SDK_EXPECTED = (251 * 3.00 + 25 * 15.00 + 27_563 * 3.75) / 1_000_000


def test_hook_counts_raw_sdk_usage_with_nested_cache_creation_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live FAIL class: the raw SDK usage dict's cache_creation is a
    nested dict. Before the fix, dict * float raised inside the pricing
    math and the never-raises hook dropped the whole call's cost — every
    fresh-cache-write composer call counted $0.00. This test feeds the
    exact shape captured from the staging log at 20:18 PT."""
    cfg = _client_config(tmp_path)
    cc = _mock_client(cfg, monkeypatch, usage_kwargs=RAW_SDK_USAGE)
    cc._voice_capped_call("a", [{"type": "text", "text": "s"}], "t")
    spend = read_spend(cfg)
    assert spend["today_usd"] == pytest.approx(RAW_SDK_EXPECTED)
    assert spend["month_usd"] == pytest.approx(RAW_SDK_EXPECTED)
