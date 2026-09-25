"""Coordination session persistence across a runtime restart (2026-06-10).

The June 3 incident: a deploy restart wiped the in-memory `_sessions`
registry mid-session. `lookup_session_by_addressee_handle` returned None
on the empty dict, so Max's reply ("can you meet teachers tomorrow at
3:00 or 3:35") was misrouted into the persona layers as if from Megha,
and the session orphaned.

These tests assert the new contract:

- Sessions write through to the per-concept file
  `coordination_sessions.json` on every mutation.
- A simulated restart (`_reset_for_tests` clears memory AND the lazy-load
  latch; the file survives) followed by a lookup WITH config hydrates the
  registry and finds the session — both for the addressee-side and
  requester-side lookups.
- A reply routed after the restart completes the session end-to-end.
- Closed sessions are not resurrected; sessions past the 24h idle
  timeout are pruned at load.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_coordination_session_persistence.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from capabilities.coordination import handler as coordination_handler
from kavi_runtime.state_per_concept import coordination_sessions_path


MEGHA = "+15555550101"
MAX = "+15555550102"


def _config_for_test(tmp_path: Path) -> dict[str, Any]:
    """Same shape as test_coordination_handler's config PLUS the
    imessage_state path that anchors the per-concept state directory."""
    return {
        "imessage": {
            "megha_phone": MEGHA,
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
        "action_layer": {"dry_run": False},
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "enable_prompt_caching": False},
        "paths": {
            "imessage_state": str(tmp_path / "state" / "imessage-state.json"),
            "eval_persona_action_intent_jsonl": str(tmp_path / "action_intent.jsonl"),
            "eval_coordinates_judgments_jsonl": str(tmp_path / "coord-judgments.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "persona-outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "persona-inbound.jsonl"),
            "outbound_blocked_jsonl": str(tmp_path / "outbound-blocked.jsonl"),
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
        },
    }


@pytest.fixture(autouse=True)
def _isolate_session_state():
    coordination_handler._reset_for_tests()
    yield
    coordination_handler._reset_for_tests()


def _make_clients() -> tuple[MagicMock, MagicMock, MagicMock]:
    claude = MagicMock()
    claude.compose_coordination_ack.return_value = {
        "text": "Got it, checking with Max now.", "char_count": 30, "_usage": None,
    }
    claude.compose_coordination_addressee_message.return_value = {
        "text": "Hey Max, can you meet the teachers tomorrow — earlier or later slot?",
        "char_count": 69, "attribution_applied": True, "_usage": None,
    }
    claude.parse_coordination_reply.return_value = {
        "branch": "4a_yes_have_it",
        "commitment_text": "Max can do the earlier slot",
        "task_title_proposal": None, "deadline": None,
        "confidence": "high", "reasoning": "clear yes", "_usage": None,
    }
    claude.compose_coordination_outcome.return_value = {
        "text": "Max said the earlier slot works.", "char_count": 32, "_usage": None,
    }
    claude.compose_conversational_reply.side_effect = AssertionError(
        "conversational composer invoked from a coordination flow"
    )
    graph = MagicMock()
    graph.create_task_in_shared_list.return_value = ("task_abc", True)
    bb = MagicMock()
    return claude, graph, bb


def _stub_send(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sends: list[dict[str, Any]] = []

    def _record(config, *, bb, text, recipient_handle, kind, chat_guid_override=None, **kwargs):
        sends.append({"text": text, "recipient": recipient_handle, "kind": kind})
        return {"sent": True, "verified": True, "blocked": False,
                "blocked_reason": None, "temp_guid": "fake-guid"}

    monkeypatch.setattr(coordination_handler, "_send_with_scanner", _record)
    return sends


def _start_session(cfg, claude, graph, bb) -> str:
    started = coordination_handler.start_coordination(
        inbound_text="can you check with Max about meeting the teachers tomorrow",
        requester_handle=MEGHA,
        addressee_handle=MAX,
        addressee_name="Max",
        coordination_ask="Can Max meet the teachers tomorrow, earlier or later slot?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert started["status"] == "coordination_started"
    return started["session_id"]


def _simulate_restart() -> None:
    """Clear all in-memory coordination state. The persisted file survives;
    the next lookup with a config lazy-loads it — exactly what a process
    restart does."""
    coordination_handler._reset_for_tests()


# ---- write-through ----------------------------------------------------------


def test_session_written_through_to_per_concept_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)

    sid = _start_session(cfg, claude, graph, bb)

    state_file = coordination_sessions_path(Path(cfg["paths"]["imessage_state"]))
    assert state_file.exists(), "write-through should have created the file"
    data = json.loads(state_file.read_text())
    assert sid in data["sessions"]
    assert data["sessions"][sid]["phase"] == "awaiting_addressee_reply"
    assert data["sessions"][sid]["addressee_handle"] == MAX


# ---- restart survival: the June 3 regression --------------------------------


def test_addressee_lookup_survives_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The exact June 3 failure: after a restart, the addressee-handle
    lookup must find the persisted session instead of returning None on an
    empty dict (which misrouted Max's reply as if from Megha)."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)
    sid = _start_session(cfg, claude, graph, bb)

    _simulate_restart()

    found = coordination_handler.lookup_session_by_addressee_handle(MAX, config=cfg)
    assert found is not None, (
        "persisted session must be found after restart — None here is the "
        "June 3 misroute"
    )
    assert found["session_id"] == sid
    assert found["phase"] == "awaiting_addressee_reply"


def test_requester_lookup_survives_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)
    sid = _start_session(cfg, claude, graph, bb)

    _simulate_restart()

    found = coordination_handler.lookup_session_by_requester_handle(MEGHA, config=cfg)
    assert found is not None
    assert found["session_id"] == sid


def test_addressee_reply_completes_session_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: restart mid-session, then Max's reply routes through the
    rehydrated session and the coordination closes normally."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    sends = _stub_send(monkeypatch)
    sid = _start_session(cfg, claude, graph, bb)

    _simulate_restart()
    # Re-stub the send recorder on the fresh module state.
    sends = _stub_send(monkeypatch)

    found = coordination_handler.lookup_session_by_addressee_handle(MAX, config=cfg)
    assert found is not None
    result = coordination_handler.handle_addressee_reply(
        session_id=found["session_id"], reply_text="earlier slot works",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert result["status"] == "coordination_closed"
    assert result["session_id"] == sid
    # Outcome went to the requester.
    assert sends[-1]["kind"] == "outcome_report"
    assert sends[-1]["recipient"] == MEGHA


# ---- pruning: closed + stale sessions don't come back -----------------------


def test_closed_session_not_resurrected_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)
    sid = _start_session(cfg, claude, graph, bb)

    closed = coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="yes that works",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert closed["status"] == "coordination_closed"

    _simulate_restart()

    assert coordination_handler.lookup_session_by_addressee_handle(MAX, config=cfg) is None
    assert coordination_handler.lookup_session_by_requester_handle(MEGHA, config=cfg) is None
    # The close was persisted: the file no longer carries the session.
    state_file = coordination_sessions_path(Path(cfg["paths"]["imessage_state"]))
    data = json.loads(state_file.read_text())
    assert sid not in data["sessions"]


def test_stale_session_pruned_on_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A persisted session older than the 24h idle timeout is pruned at
    load, not resurrected (matches the in-memory idle-purge convention in
    the capability spec)."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)
    sid = _start_session(cfg, claude, graph, bb)

    # Backdate the persisted ts_started past the idle timeout.
    state_file = coordination_sessions_path(Path(cfg["paths"]["imessage_state"]))
    data = json.loads(state_file.read_text())
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    data["sessions"][sid]["ts_started"] = stale_ts
    state_file.write_text(json.dumps(data))

    _simulate_restart()

    assert coordination_handler.lookup_session_by_addressee_handle(MAX, config=cfg) is None
    # The pruned view was written back: file no longer carries the session.
    data_after = json.loads(state_file.read_text())
    assert sid not in data_after["sessions"]


# ---- no-path configs degrade to in-memory-only ------------------------------


def test_missing_state_path_degrades_to_in_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Configs without paths.imessage_state (minimal test configs) keep the
    pre-persistence behavior: sessions work in-memory, nothing raises, and
    nothing is written to disk."""
    cfg = _config_for_test(tmp_path)
    del cfg["paths"]["imessage_state"]
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)

    sid = _start_session(cfg, claude, graph, bb)
    found = coordination_handler.lookup_session_by_addressee_handle(MAX, config=cfg)
    assert found is not None and found["session_id"] == sid
    assert not (tmp_path / "state").exists()
