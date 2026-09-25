"""Tests for the periodic_summary composer + impl wiring of the two
2026-06-10 features: evening close suggestions and morning themes.

Contract locked here:

  * The composer marshals `close_suggestions` ([{title, reason}]) and
    `theme` ({label, task_count}) into the LLM input payload; the
    numeric naming cap is surfaced from
    structural_checks.CLOSE_SUGGESTIONS_SURFACE_MAX in the user_msg (the
    max-N rule itself is BEHAVIOR, owned by the skill).
  * Null theme / empty suggestions leave the input payload shape
    byte-identical to the pre-feature contract (additive keys only).
  * Impl: Megha's 9 PM rollup computes close suggestions (v0: her
    mailbox only — Max's rollup never does); morning fires the theme
    selector per recipient; the rollup never does.
  * New inputs feed the debounce hash; empty inputs leave legacy hash
    values stable.
  * Surfaced suggestions are recorded after a successful LLM compose +
    send, and NOT recorded when the composer fell back to the safe
    sentence.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_periodic_summary_close_theme_marshaling.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.structural_checks import CLOSE_SUGGESTIONS_SURFACE_MAX
from capabilities.kavi_persona import close_suggestions as _cs_module
from capabilities.kavi_persona import selection as _selection_module
from capabilities.kavi_persona.composers import periodic_summary as _ps_module
from capabilities.kavi_persona.composers.periodic_summary import (
    compose_periodic_summary,
    _periodic_summary_input_hash,
)

from kavi_runtime.runtime import outbound_scanner as _outbound_scanner

# Test-owned fictional household handles. Pinned here so these tests do not
# depend on the real handles the runtime allowlist is configured with.
_TEST_HOUSEHOLD_HANDLES = {
    "+15555550101", "+15555550102", "megha@example.com", "max@example.com",
}


@pytest.fixture(autouse=True)
def _test_household_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _outbound_scanner, "is_household_handle",
        lambda h: bool(h) and h.strip().lower() in _TEST_HOUSEHOLD_HANDLES,
    )


MEGHA_PHONE = "+15555550101"
MAX_PHONE = "+15555550102"

_TODAY_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT08:00:00Z")

_SUGGESTIONS = [
    {"task_id": "t-mj", "title": "MJ Pay Boonli invoice",
     "reason": "your reply says it was paid", "reply_id": "r-1"},
]
_THEME = {"label": "Summer camp planning", "task_count": 4}


# ---- composer marshaling -------------------------------------------------------


def _fake_compose_client() -> MagicMock:
    """Same client double as tests/test_periodic_summary_per_person.py:
    captures the Anthropic call instead of executing it."""
    client = MagicMock()
    client._build_system_prompt.return_value = []
    client._model_for_call_type.return_value = "claude-sonnet-4-6"
    client._log_call_start.return_value = 0.0
    resp = MagicMock()
    resp.content = [MagicMock(text='{"message": "ok"}')]
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"input_tokens": 1, "output_tokens": 1}
    client._anthropic.messages.create.return_value = resp
    client._extract_json.return_value = {"message": "ok"}
    return client


def _payload_and_msg(client: MagicMock) -> tuple[dict[str, Any], str]:
    user_msg = client._anthropic.messages.create.call_args.kwargs["messages"][0]["content"]
    payload = json.loads(user_msg.split("<input>\n", 1)[1].split("\n</input>")[0])
    return payload, user_msg


def test_composer_marshals_close_suggestions_into_llm_input() -> None:
    client = _fake_compose_client()
    compose_periodic_summary(
        client,
        queued_tasks=[], pending_questions=[],
        is_rollup=True, time_of_day="9pm",
        close_suggestions=_SUGGESTIONS,
    )
    payload, user_msg = _payload_and_msg(client)
    assert payload["close_suggestions"] == [
        {"title": "MJ Pay Boonli invoice",
         "reason": "your reply says it was paid"},
    ]
    # The naming cap reaches the LLM via the user_msg, sourced from the
    # structural constant — never from a literal in the skill.
    assert f"at most {CLOSE_SUGGESTIONS_SURFACE_MAX}" in user_msg


def test_composer_marshals_theme_into_llm_input() -> None:
    client = _fake_compose_client()
    compose_periodic_summary(
        client,
        queued_tasks=[], pending_questions=[],
        is_rollup=False, time_of_day="morning",
        theme=_THEME,
    )
    payload, _ = _payload_and_msg(client)
    assert payload["theme"] == {"label": "Summer camp planning", "task_count": 4}


def test_null_theme_and_empty_suggestions_leave_payload_shape_unchanged() -> None:
    """Additive contract: callers without the new features produce a
    payload byte-identical in shape to the pre-2026-06-10 one."""
    baseline_client = _fake_compose_client()
    compose_periodic_summary(
        baseline_client,
        queued_tasks=[], pending_questions=[],
        is_rollup=False, time_of_day="morning",
    )
    baseline_payload, baseline_msg = _payload_and_msg(baseline_client)

    client = _fake_compose_client()
    compose_periodic_summary(
        client,
        queued_tasks=[], pending_questions=[],
        is_rollup=False, time_of_day="morning",
        close_suggestions=[], theme=None,
    )
    payload, user_msg = _payload_and_msg(client)

    assert "theme" not in payload
    assert "close_suggestions" not in payload
    assert payload == baseline_payload
    assert user_msg == baseline_msg


# ---- debounce hash --------------------------------------------------------------


def test_empty_new_inputs_keep_legacy_hash_stable() -> None:
    queued = [{"title": "MJ Book dentist"}]
    legacy = _periodic_summary_input_hash(queued, [], [], due_soon=[])
    extended = _periodic_summary_input_hash(
        queued, [], [], due_soon=[], close_suggestions=[], theme=None,
    )
    assert legacy == extended


def test_close_suggestions_and_theme_bust_the_hash() -> None:
    base = _periodic_summary_input_hash([], [], [], due_soon=[])
    with_close = _periodic_summary_input_hash(
        [], [], [], due_soon=[], close_suggestions=_SUGGESTIONS,
    )
    with_theme = _periodic_summary_input_hash(
        [], [], [], due_soon=[], theme=_THEME,
    )
    assert base != with_close
    assert base != with_theme
    assert with_close != with_theme


def test_newer_reply_on_same_suggestion_busts_the_hash() -> None:
    a = _periodic_summary_input_hash(
        [], [], [],
        close_suggestions=[{"title": "MJ Pay Boonli invoice", "reply_id": "r-1"}],
    )
    b = _periodic_summary_input_hash(
        [], [], [],
        close_suggestions=[{"title": "MJ Pay Boonli invoice", "reply_id": "r-2"}],
    )
    assert a != b


# ---- impl wiring (scheduler entry point) ----------------------------------------


def _base_config(tmp_path: Path) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return {
        "imessage": {"megha_phone": MEGHA_PHONE, "max_phone": MAX_PHONE},
        "graph": {"mstodo_shared_list_id": "AQTEST=="},
        "paths": {
            "imessage_state": str(state_dir / "imessage-state.json"),
            "runs_jsonl": str(tmp_path / "runs.jsonl"),
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            "runtime_events_jsonl": str(tmp_path / "runtime-events.jsonl"),
        },
    }


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Same stub registry pattern as tests/test_periodic_summary_per_person.py
    plus capture stubs for the two new selectors."""
    reg: dict[str, Any] = {
        "sends": [],
        "open_tasks": [],
        "completed_tasks": [],
        "queued_seq": [],
        "pending_seq": [],
        "close_calls": [],
        "close_result": [],
        "theme_calls": [],
        "theme_result": None,
        "recorded_surfaced": [],
    }

    def _send(
        config: dict, text: str, *, kind: str,
        recipient_handle: str | None = None,
        suppress_outlook_fallback: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        reg["sends"].append({"text": text, "recipient_handle": recipient_handle})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send)
    monkeypatch.setattr(_ps_module, "_send_imessage_with_fallback", _send)

    def _save_anchor(path, recipient_handle, **kwargs):
        return None

    monkeypatch.setattr(handlers, "save_summary_anchor", _save_anchor)
    monkeypatch.setattr(_ps_module, "save_summary_anchor", _save_anchor)

    def _drain(_path):
        return reg["queued_seq"].pop(0) if reg["queued_seq"] else []

    def _pendings(_path):
        return reg["pending_seq"].pop(0) if reg["pending_seq"] else []

    monkeypatch.setattr(handlers, "drain_summary_queue", _drain)
    monkeypatch.setattr(_ps_module, "drain_summary_queue", _drain)
    monkeypatch.setattr(handlers, "list_pending_questions", _pendings)
    monkeypatch.setattr(_ps_module, "list_pending_questions", _pendings)

    graph = MagicMock()
    graph.list_open_todo_tasks.side_effect = lambda *a, **kw: list(reg["open_tasks"])
    graph.list_completed_todo_tasks.side_effect = lambda *a, **kw: list(reg["completed_tasks"])

    claude = MagicMock()
    claude.compose_periodic_summary.return_value = "Stub digest content here."
    reg["claude"] = claude

    _stub_clients = lambda c: (graph, claude, MagicMock())
    monkeypatch.setattr(handlers, "_get_clients", _stub_clients)
    monkeypatch.setattr(_ps_module, "_get_clients", _stub_clients)
    from kavi_runtime.runtime import clients as _clients_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_clients)

    # The impl imports these at call time from their canonical modules, so
    # patching the module attributes intercepts the calls.
    def _select_close(
        config: dict, open_tasks: Any, *,
        account: str | None = None, own_addresses: Any = None,
    ) -> list[dict[str, Any]]:
        reg["close_calls"].append({"open_tasks": open_tasks, "account": account})
        return list(reg["close_result"])

    monkeypatch.setattr(_cs_module, "select_close_suggestions", _select_close)

    def _record(config: dict, suggestions: list[dict[str, Any]]) -> None:
        reg["recorded_surfaced"].append(list(suggestions))

    monkeypatch.setattr(
        _cs_module, "record_close_suggestions_surfaced", _record,
    )

    def _select_theme(config: dict, open_tasks: Any, *, recipient: str,
                      now: Any = None) -> dict[str, Any] | None:
        reg["theme_calls"].append({"recipient": recipient, "open_tasks": open_tasks})
        return reg["theme_result"]

    monkeypatch.setattr(_selection_module, "_select_morning_theme", _select_theme)

    return reg


def _open_task(task_id: str, title: str) -> dict[str, Any]:
    return {"id": task_id, "title": title, "createdDateTime": _TODAY_ISO}


def test_megha_rollup_computes_and_composes_close_suggestions(
    tmp_path: Path, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _base_config(tmp_path)
    # Pin fictional per-person mailboxes so the test owns the account keys.
    monkeypatch.setattr(_ps_module, "_PERSON_MAILBOX", {
        "megha": {"account": "megha@example.com", "own": {"megha@example.com"}},
        "max": {"account": "max@example.com", "own": {"max@example.com"}},
    }, raising=False)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Pay Boonli invoice"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Pay Boonli invoice"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])
    harness["close_result"] = list(_SUGGESTIONS)

    handlers.periodic_summary(cfg, is_rollup=True)

    # 2026-06-22: the selector runs for BOTH persons, each over THEIR OWN
    # tasks and THEIR OWN mailbox account (so a Max-owned done task asks Max).
    assert len(harness["close_calls"]) == 2
    by_account = {
        c["account"]: [t["title"] for t in c["open_tasks"]]
        for c in harness["close_calls"]
    }
    assert by_account["megha@example.com"] == ["MJ Pay Boonli invoice"]
    assert by_account["max@example.com"] == ["MM Leave cleaner cash"]

    megha_call, max_call = harness["claude"].compose_periodic_summary.call_args_list
    assert megha_call.kwargs["recipient_name"] == "Megha"
    assert megha_call.kwargs["close_suggestions"] == _SUGGESTIONS
    assert max_call.kwargs["recipient_name"] == "Max"
    # Max now gets his own close-suggestions too (both-persons enablement).
    assert max_call.kwargs["close_suggestions"] == _SUGGESTIONS


def test_morning_never_computes_close_suggestions(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Book dentist"}])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=False)

    assert harness["close_calls"] == []


def test_morning_computes_theme_per_recipient(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Register for camp"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Register for camp"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])
    harness["theme_result"] = dict(_THEME)

    handlers.periodic_summary(cfg, is_rollup=False)

    recipients = [c["recipient"] for c in harness["theme_calls"]]
    assert recipients == ["megha", "max"]
    # Each recipient's theme runs over their OWN tasks.
    assert [t["title"] for t in harness["theme_calls"][0]["open_tasks"]] == [
        "MJ Register for camp",
    ]
    megha_call = harness["claude"].compose_periodic_summary.call_args_list[0]
    assert megha_call.kwargs["theme"] == _THEME


def test_rollup_never_computes_theme(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Book dentist"}])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    assert harness["theme_calls"] == []
    megha_call = harness["claude"].compose_periodic_summary.call_args
    assert megha_call.kwargs["theme"] is None


def test_theme_alone_does_not_trigger_max_send(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """A theme is a framing device, not new information: Max's all-quiet
    skip happens BEFORE theme selection, so his skipped morning never pays
    a clustering call."""
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])
    harness["theme_result"] = dict(_THEME)

    handlers.periodic_summary(cfg, is_rollup=False)

    recipients = [c["recipient"] for c in harness["theme_calls"]]
    assert recipients == ["megha"], "Max skipped → no clustering call for him"
    assert len(harness["sends"]) == 1


def test_surfaced_suggestions_recorded_after_successful_compose_and_send(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Pay Boonli invoice"}])
    harness["pending_seq"].append([])
    harness["close_result"] = list(_SUGGESTIONS)

    handlers.periodic_summary(cfg, is_rollup=True)

    assert harness["recorded_surfaced"] == [_SUGGESTIONS]


def test_surfaced_not_recorded_when_compose_falls_back(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """LLM failure → safe sentence carries no suggestions → the one-shot
    surfacing state must NOT be burned (tomorrow re-suggests)."""
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Pay Boonli invoice"}])
    harness["pending_seq"].append([])
    harness["close_result"] = list(_SUGGESTIONS)
    harness["claude"].compose_periodic_summary.return_value = None

    handlers.periodic_summary(cfg, is_rollup=True)

    assert harness["recorded_surfaced"] == []
    assert len(harness["sends"]) == 1  # safe fallback still went out


def test_close_selector_failure_never_blocks_the_rollup(
    tmp_path: Path, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Pay Boonli invoice"}])
    harness["pending_seq"].append([])

    def _boom(config: dict, open_tasks: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("selector exploded")

    monkeypatch.setattr(_cs_module, "select_close_suggestions", _boom)

    handlers.periodic_summary(cfg, is_rollup=True)

    assert len(harness["sends"]) == 1
    megha_call = harness["claude"].compose_periodic_summary.call_args
    assert megha_call.kwargs["close_suggestions"] == []


def test_theme_selector_failure_never_blocks_the_morning(
    tmp_path: Path, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Book dentist"}])
    harness["pending_seq"].append([])

    def _boom(config: dict, open_tasks: Any, *, recipient: str,
              now: Any = None) -> dict[str, Any] | None:
        raise RuntimeError("clusterer exploded")

    monkeypatch.setattr(_selection_module, "_select_morning_theme", _boom)

    handlers.periodic_summary(cfg, is_rollup=False)

    assert len(harness["sends"]) == 1
    megha_call = harness["claude"].compose_periodic_summary.call_args
    assert megha_call.kwargs["theme"] is None
