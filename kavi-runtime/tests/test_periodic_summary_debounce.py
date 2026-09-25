"""periodic_summary debounce on identical composer input.

Why this exists: 2026-05-27. Kavi's 9 PM iMessage was firing nearly identical
content twice daily for at least three fires, surfacing the same five
"pending facts to confirm." Root cause: the runtime ran the composer LLM on
every cron tick regardless of whether the upstream state had changed since
the prior fire.

The fix introduces a stable SHA256-prefix hash over the composer's INPUT
payload (queued task titles + pending Q&A titles + pending fact topics,
sorted deterministically). Before send, the runtime compares to the prior
fire's stored hash. If they match AND the prior fire was < 36 hours ago,
this send is suppressed and a `periodic_summary_suppressed` row is logged.

The 36-hour ceiling guarantees Megha still gets a heartbeat at least every
~36 hours even if upstream state is truly unchanged for two days.

Tests:
  - Two consecutive fires with identical inputs: second is suppressed; an
    outbound row with kind=periodic_summary_suppressed is logged.
  - Two consecutive fires with different inputs: both send.
  - Identical inputs separated by 37 hours: second sends (debounce expires).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_periodic_summary_debounce.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers


def _base_config(tmp_path: Path) -> dict[str, Any]:
    """Minimal config wiring all the paths periodic_summary touches."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    evals_dir = tmp_path / "evals" / "kavi-persona"
    evals_dir.mkdir(parents=True)
    return {
        "imessage": {"megha_phone": "+15555550101"},
        "graph": {"mstodo_shared_list_id": "AQTEST=="},
        "paths": {
            "imessage_state": str(state_dir / "imessage-state.json"),
            "runs_jsonl": str(tmp_path / "runs.jsonl"),
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(
                evals_dir / "eval-persona-outbound-judgments.jsonl"
            ),
            "runtime_events_jsonl": str(tmp_path / "runtime-events.jsonl"),
        },
    }


@pytest.fixture
def stub_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the LLM, send wrapper, queue-drain, pending Q&A, and anchor save
    so the test can control the composer's INPUT shape (queued / pending /
    pending_facts) deterministically. Returns the stub registry so tests
    can mutate fixture state between fires."""
    sent: list[str] = []
    queued_seq: list[list[dict[str, Any]]] = []
    pending_seq: list[list[dict[str, Any]]] = []

    def _send(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append(text)
        return {"verified": True, "fallback_used": False}

    # Phase 4 (2026-06-02): periodic_summary moved to
    # capabilities/kavi_persona/composers/periodic_summary.py — patch there
    # too so the impl reaches the stub bindings.
    from capabilities.kavi_persona.composers import periodic_summary as _ps_module
    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send)
    monkeypatch.setattr(_ps_module, "_send_imessage_with_fallback", _send)
    monkeypatch.setattr(handlers, "save_summary_anchor", lambda *a, **kw: None)
    monkeypatch.setattr(_ps_module, "save_summary_anchor", lambda *a, **kw: None)

    def _drain_summary_queue(_path: Path) -> list[dict[str, Any]]:
        return queued_seq.pop(0) if queued_seq else []

    def _list_pending_questions(_path: Path) -> list[dict[str, Any]]:
        return pending_seq.pop(0) if pending_seq else []

    monkeypatch.setattr(handlers, "drain_summary_queue", _drain_summary_queue)
    monkeypatch.setattr(handlers, "list_pending_questions", _list_pending_questions)
    monkeypatch.setattr(_ps_module, "drain_summary_queue", _drain_summary_queue)
    monkeypatch.setattr(_ps_module, "list_pending_questions", _list_pending_questions)

    claude = MagicMock()
    claude.compose_periodic_summary.return_value = (
        "Quiet morning. 1 task queued, 0 pending. We go again."
    )
    monkeypatch.setattr(
        handlers, "_get_clients",
        lambda c: (MagicMock(), claude, MagicMock()),
    )
    monkeypatch.setattr(
        _ps_module, "_get_clients",
        lambda c: (MagicMock(), claude, MagicMock()),
    )

    return {
        "sent": sent,
        "queued_seq": queued_seq,
        "pending_seq": pending_seq,
        "claude": claude,
    }


def _read_outbound_rows(config: dict) -> list[dict[str, Any]]:
    path = Path(config["paths"]["eval_persona_outbound_judgments_jsonl"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---- identical inputs → suppress -------------------------------------------


def test_identical_inputs_suppress_second_fire(
    tmp_path: Path, stub_runtime: dict[str, Any],
) -> None:
    """Two consecutive fires with the same queued + pending + facts input
    must produce: first send, then suppress + log."""
    cfg = _base_config(tmp_path)
    sent = stub_runtime["sent"]

    queued = [{"title": "Buy beans for Nadia", "owner": "Megha", "is_priority": False}]
    pending: list[dict[str, Any]] = []

    # Two consecutive fires, identical input on both.
    stub_runtime["queued_seq"].extend([list(queued), list(queued)])
    stub_runtime["pending_seq"].extend([list(pending), list(pending)])

    handlers.periodic_summary(cfg, is_rollup=True)
    handlers.periodic_summary(cfg, is_rollup=True)

    # Only one outbound message sent to Megha.
    assert len(sent) == 1, f"expected 1 sent, got {len(sent)}"

    # The suppression row landed in outbound-judgments.
    rows = _read_outbound_rows(cfg)
    suppression_rows = [r for r in rows if r.get("kind") == "periodic_summary_suppressed"]
    assert len(suppression_rows) == 1
    sup = suppression_rows[0]
    assert sup["context"]["suppression_reason"] == "identical_input_hash"
    assert sup["context"]["input_hash"]
    assert sup["context"]["prior_fire_ts"]


def test_identical_inputs_persist_hash_to_state_file(
    tmp_path: Path, stub_runtime: dict[str, Any],
) -> None:
    """After the first fire, the hash state file must exist with the
    composed input's hash. The debounce checks this file on next fire."""
    cfg = _base_config(tmp_path)
    queued = [{"title": "Confirm Cleaner cash", "owner": "Max", "is_priority": True}]
    stub_runtime["queued_seq"].extend([list(queued)])
    stub_runtime["pending_seq"].extend([[]])

    handlers.periodic_summary(cfg, is_rollup=False)

    state_file = Path(cfg["paths"]["imessage_state"]).parent / "periodic_summary_last_hash.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert "hash" in data and len(data["hash"]) == 16
    assert "ts" in data
    assert data["kind"] in {"summary", "rollup"}


# ---- different inputs → both send ------------------------------------------


def test_different_inputs_both_send(
    tmp_path: Path, stub_runtime: dict[str, Any],
) -> None:
    """When the input payload differs between two fires, both messages
    must ship — debounce only suppresses true duplicates."""
    cfg = _base_config(tmp_path)
    sent = stub_runtime["sent"]

    stub_runtime["queued_seq"].extend([
        [{"title": "Pay Cleaner cash", "owner": "Megha", "is_priority": False}],
        [{"title": "Order diapers", "owner": "Megha", "is_priority": False}],
    ])
    stub_runtime["pending_seq"].extend([[], []])

    handlers.periodic_summary(cfg, is_rollup=False)
    handlers.periodic_summary(cfg, is_rollup=False)

    assert len(sent) == 2

    # No suppression row.
    rows = _read_outbound_rows(cfg)
    suppression_rows = [r for r in rows if r.get("kind") == "periodic_summary_suppressed"]
    assert suppression_rows == []


# ---- 36-hour ceiling -------------------------------------------------------


def test_identical_inputs_after_36_hours_send(
    tmp_path: Path, stub_runtime: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with identical inputs, after 36 hours the next fire must send —
    Megha gets a heartbeat instead of indefinite silence on a truly
    unchanged state."""
    cfg = _base_config(tmp_path)
    sent = stub_runtime["sent"]

    queued = [{"title": "Bayview registration", "owner": "Megha", "is_priority": False}]
    stub_runtime["queued_seq"].extend([list(queued), list(queued)])
    stub_runtime["pending_seq"].extend([[], []])

    # First fire writes today's hash.
    handlers.periodic_summary(cfg, is_rollup=True)
    assert len(sent) == 1

    # Forge the on-disk last-hash state to be 37 hours old. This is exactly
    # what the runtime would see if the upstream state genuinely hadn't
    # changed for over a day.
    state_file = Path(cfg["paths"]["imessage_state"]).parent / "periodic_summary_last_hash.json"
    data = json.loads(state_file.read_text())
    aged_ts = (
        datetime.now(timezone.utc) - timedelta(hours=37)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["ts"] = aged_ts
    state_file.write_text(json.dumps(data, indent=2))

    # Second fire, identical input, but the prior hash is "old enough"
    # that the debounce ceiling has expired.
    handlers.periodic_summary(cfg, is_rollup=True)
    assert len(sent) == 2, "37h-old identical-hash fire must send"


# ---- hash stability --------------------------------------------------------


def test_hash_is_stable_across_list_order(tmp_path: Path) -> None:
    """The dedup hash must be order-independent — the LLM doesn't care
    which order queued items arrive in, and a flapping order in the
    upstream queue shouldn't bust the debounce."""
    a = handlers._periodic_summary_input_hash(
        queued=[{"title": "Beans"}, {"title": "Cleaner cash"}],
        pending=[],
        pending_facts=[{"topic": "Max"}, {"topic": "Nadia"}],
    )
    b = handlers._periodic_summary_input_hash(
        queued=[{"title": "Cleaner cash"}, {"title": "Beans"}],
        pending=[],
        pending_facts=[{"topic": "Nadia"}, {"topic": "Max"}],
    )
    assert a == b
    assert len(a) == 16


def test_hash_changes_when_content_changes(tmp_path: Path) -> None:
    """Changing the actual content must produce a different hash."""
    a = handlers._periodic_summary_input_hash(
        queued=[{"title": "Beans"}],
        pending=[],
        pending_facts=[],
    )
    b = handlers._periodic_summary_input_hash(
        queued=[{"title": "Diapers"}],
        pending=[],
        pending_facts=[],
    )
    assert a != b
