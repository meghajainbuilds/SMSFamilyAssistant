"""Tests for the 2026-06-11 stale-task nudge (PM feature request from the
morning-digest feedback: "picking one or two tasks from an old list based
on how long they have been open").

Contract locked here:

  * Selection — `_select_stale_task_nudge` picks the recipient's 1-2
    longest-open tasks (strictly older than `STALE_TASK_MIN_AGE_DAYS`
    Pacific calendar days by createdDateTime), oldest first; null /
    unparseable createdDateTime skipped.
  * Composer — `stale_tasks` is marshaled into the LLM input payload
    ONLY when non-empty (additive key; legacy payload shape unchanged).
  * Impl — the nudge is computed per recipient, morning only, and ONLY
    when the cluster theme came back None (theme wins; mutual exclusion
    by construction). The rollup never computes it. Selector failure
    never blocks the digest.
  * Debounce hash — stale items hash as (title, age_days) pairs; empty
    leaves legacy hashes stable; the daily age_days increment keeps the
    repeat-every-morning behavior from being debounced (same contract
    as due_soon).
  * Deep verify — the `stale_task_dropped` gate fires when the composed
    morning digest drops a payload item, passes when every item is
    referenced, and no-ops for rollups / None output. The verify and
    replay routes thread + echo the field.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_stale_task_nudge.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kavi_runtime import handlers, synthetic_compose
from capabilities.kavi_persona import selection as _selection_module
from capabilities.kavi_persona.selection import (
    STALE_TASK_MIN_AGE_DAYS,
    _select_stale_task_nudge,
)
from capabilities.kavi_persona.verify import SELECTION_GATES
from capabilities.kavi_persona.composers import periodic_summary as _ps_module
from capabilities.kavi_persona.composers.periodic_summary import (
    compose_periodic_summary,
    _periodic_summary_input_hash,
)

MEGHA_PHONE = "+15555550101"
MAX_PHONE = "+15555550102"

_NOW = datetime(2026, 6, 11, 14, 0, 0, tzinfo=timezone.utc)  # 7am Pacific


def _task(title: str, *, age_days: int | None = None,
          created: str | None = None) -> dict[str, Any]:
    if created is None and age_days is not None:
        created = (_NOW - timedelta(days=age_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
    out: dict[str, Any] = {"id": f"t-{title[:12]}", "title": title}
    if created is not None:
        out["createdDateTime"] = created
    return out


_STALE = [
    {"task_id": "t-sct", "title": "MJ Submit SCT reimbursement ticket", "age_days": 45},
    {"task_id": "t-lib", "title": "MJ Renew library card", "age_days": 21},
]


# ---- selection: _select_stale_task_nudge ------------------------------------


def test_constant_value_is_two_weeks() -> None:
    """The threshold the spec + skill reference by name."""
    assert STALE_TASK_MIN_AGE_DAYS == 14


def test_age_threshold_is_strictly_greater_than_constant() -> None:
    """'Open longer than 14 days' is strict: exactly 14 days is normal
    backlog (excluded); day 15 qualifies."""
    tasks = [
        _task("MJ At threshold", age_days=STALE_TASK_MIN_AGE_DAYS),
        _task("MJ Past threshold", age_days=STALE_TASK_MIN_AGE_DAYS + 1),
    ]
    out = _select_stale_task_nudge(tasks, now=_NOW)
    assert [s["title"] for s in out] == ["MJ Past threshold"]
    assert out[0]["age_days"] == STALE_TASK_MIN_AGE_DAYS + 1


def test_sorted_oldest_first() -> None:
    tasks = [
        _task("MJ Newer stale", age_days=21),
        _task("MJ Oldest stale", age_days=45),
    ]
    out = _select_stale_task_nudge(tasks, now=_NOW)
    assert [s["age_days"] for s in out] == [45, 21]
    assert out[0]["title"] == "MJ Oldest stale"


def test_max_two_items_by_default() -> None:
    tasks = [
        _task("MJ Stale a", age_days=20),
        _task("MJ Stale b", age_days=45),
        _task("MJ Stale c", age_days=30),
    ]
    out = _select_stale_task_nudge(tasks, now=_NOW)
    assert [s["age_days"] for s in out] == [45, 30]


def test_max_items_override() -> None:
    tasks = [_task(f"MJ Stale {i}", age_days=20 + i) for i in range(3)]
    out = _select_stale_task_nudge(tasks, now=_NOW, max_items=1)
    assert len(out) == 1
    assert out[0]["age_days"] == 22


def test_age_counts_pacific_calendar_days_not_utc() -> None:
    """now = 02:00 UTC Jun 11 is still Jun 10 in Seattle; a task created
    midday UTC May 27 is 14 Pacific days old (excluded) even though naive
    UTC date math says 15 (would be included)."""
    now = datetime(2026, 6, 11, 2, 0, 0, tzinfo=timezone.utc)
    tasks = [_task("MJ Pacific boundary", created="2026-05-27T12:00:00Z")]
    assert _select_stale_task_nudge(tasks, now=now) == []


def test_null_and_unparseable_created_skipped() -> None:
    """Unknown age is not 'old' — same contract as _parse_graph_iso."""
    tasks = [
        _task("MJ No created field"),
        {"id": "t-null", "title": "MJ Null created", "createdDateTime": None},
        {"id": "t-bad", "title": "MJ Bad created",
         "createdDateTime": "not-a-timestamp"},
        _task("MJ Real stale", age_days=30),
    ]
    out = _select_stale_task_nudge(tasks, now=_NOW)
    assert [s["title"] for s in out] == ["MJ Real stale"]


def test_empty_title_skipped_and_empty_inputs_safe() -> None:
    assert _select_stale_task_nudge(None, now=_NOW) == []
    assert _select_stale_task_nudge([], now=_NOW) == []
    tasks = [{"id": "t-x", "title": "  ",
              "createdDateTime": "2026-04-01T00:00:00Z"}]
    assert _select_stale_task_nudge(tasks, now=_NOW) == []


# ---- composer marshaling -----------------------------------------------------


def _fake_compose_client() -> MagicMock:
    """Same client double as tests/test_periodic_summary_close_theme_marshaling.py."""
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


def test_composer_marshals_stale_tasks_into_llm_input() -> None:
    client = _fake_compose_client()
    compose_periodic_summary(
        client,
        queued_tasks=[], pending_questions=[],
        is_rollup=False, time_of_day="morning",
        stale_tasks=_STALE,
    )
    payload, _ = _payload_and_msg(client)
    # The composer projects to {title, age_days} for the LLM — task_id is
    # selection plumbing (it rides to registration, not into the prompt).
    assert payload["stale_tasks"] == [
        {"title": s["title"], "age_days": s["age_days"]} for s in _STALE
    ]


def test_empty_stale_tasks_leave_payload_shape_unchanged() -> None:
    """Additive contract: no stale input → no key, byte-identical payload."""
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
        stale_tasks=[],
    )
    payload, user_msg = _payload_and_msg(client)

    assert "stale_tasks" not in payload
    assert payload == baseline_payload
    assert user_msg == baseline_msg


# ---- debounce hash -------------------------------------------------------------


def test_empty_stale_tasks_keep_legacy_hash_stable() -> None:
    queued = [{"title": "MJ Book dentist"}]
    legacy = _periodic_summary_input_hash(queued, [], [], due_soon=[])
    extended = _periodic_summary_input_hash(
        queued, [], [], due_soon=[], stale_tasks=[],
    )
    assert legacy == extended


def test_stale_tasks_bust_the_hash() -> None:
    base = _periodic_summary_input_hash([], [], [], due_soon=[])
    with_stale = _periodic_summary_input_hash(
        [], [], [], due_soon=[], stale_tasks=_STALE,
    )
    assert base != with_stale


def test_daily_age_increment_busts_the_hash() -> None:
    """age_days increments every day, so the intentional repeat-every-
    morning-until-closed behavior is never debounced (due_soon contract)."""
    day1 = _periodic_summary_input_hash(
        [], [], [],
        stale_tasks=[{"title": "MJ Submit SCT ticket", "age_days": 45}],
    )
    day2 = _periodic_summary_input_hash(
        [], [], [],
        stale_tasks=[{"title": "MJ Submit SCT ticket", "age_days": 46}],
    )
    assert day1 != day2


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
    """Same stub registry pattern as
    tests/test_periodic_summary_close_theme_marshaling.py plus capture
    stubs for the theme + stale selectors."""
    reg: dict[str, Any] = {
        "sends": [],
        "open_tasks": [],
        "queued_seq": [],
        "pending_seq": [],
        "theme_result": None,
        "stale_calls": [],
        "stale_result": [],
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

    monkeypatch.setattr(handlers, "save_summary_anchor", lambda *a, **kw: None)
    monkeypatch.setattr(_ps_module, "save_summary_anchor", lambda *a, **kw: None)

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
    graph.list_completed_todo_tasks.side_effect = lambda *a, **kw: []

    claude = MagicMock()
    claude.compose_periodic_summary.return_value = "Stub digest content here."
    reg["claude"] = claude

    _stub_clients = lambda c: (graph, claude, MagicMock())
    monkeypatch.setattr(handlers, "_get_clients", _stub_clients)
    monkeypatch.setattr(_ps_module, "_get_clients", _stub_clients)
    from kavi_runtime.runtime import clients as _clients_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_clients)

    def _select_theme(config: dict, open_tasks: Any, *, recipient: str,
                      now: Any = None) -> dict[str, Any] | None:
        return reg["theme_result"]

    monkeypatch.setattr(_selection_module, "_select_morning_theme", _select_theme)

    def _select_stale(open_tasks: Any, *, now: Any = None,
                      max_items: int = 2) -> list[dict[str, Any]]:
        reg["stale_calls"].append({"open_tasks": open_tasks})
        return list(reg["stale_result"])

    monkeypatch.setattr(
        _selection_module, "_select_stale_task_nudge", _select_stale,
    )

    return reg


def _open_task(task_id: str, title: str) -> dict[str, Any]:
    return {
        "id": task_id, "title": title,
        "createdDateTime": (_NOW - timedelta(days=45)).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        ),
    }


def test_morning_no_theme_computes_nudge_per_recipient_over_own_tasks(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Submit SCT reimbursement ticket"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Submit SCT reimbursement ticket"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])
    harness["theme_result"] = None
    harness["stale_result"] = list(_STALE)

    handlers.periodic_summary(cfg, is_rollup=False)

    # One nudge selection per recipient, each over their OWN open tasks.
    assert len(harness["stale_calls"]) == 2
    assert [t["title"] for t in harness["stale_calls"][0]["open_tasks"]] == [
        "MJ Submit SCT reimbursement ticket",
    ]
    assert [t["title"] for t in harness["stale_calls"][1]["open_tasks"]] == [
        "MM Leave cleaner cash",
    ]
    megha_call, max_call = harness["claude"].compose_periodic_summary.call_args_list
    assert megha_call.kwargs["recipient_name"] == "Megha"
    assert megha_call.kwargs["stale_tasks"] == _STALE
    assert max_call.kwargs["stale_tasks"] == _STALE


def test_theme_present_suppresses_the_nudge(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """One top-of-mind frame per morning: when the theme fired, the stale
    selector is never even called (mutual exclusion by construction)."""
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [_open_task("t-mj", "MJ Register for camp")]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Register for camp"},
    ])
    harness["pending_seq"].append([])
    harness["theme_result"] = {"label": "Summer camp planning", "task_count": 4}
    harness["stale_result"] = list(_STALE)

    handlers.periodic_summary(cfg, is_rollup=False)

    assert harness["stale_calls"] == []
    megha_call = harness["claude"].compose_periodic_summary.call_args_list[0]
    assert megha_call.kwargs["theme"] == {
        "label": "Summer camp planning", "task_count": 4,
    }
    assert megha_call.kwargs["stale_tasks"] == []


def test_rollup_never_computes_the_nudge(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [_open_task("t-mj", "MJ Submit SCT ticket")]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Submit SCT ticket"},
    ])
    harness["pending_seq"].append([])
    harness["stale_result"] = list(_STALE)

    handlers.periodic_summary(cfg, is_rollup=True)

    assert harness["stale_calls"] == []
    megha_call = harness["claude"].compose_periodic_summary.call_args
    assert megha_call.kwargs["stale_tasks"] == []


def test_stale_selector_failure_never_blocks_the_morning(
    tmp_path: Path, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Book dentist"}])
    harness["pending_seq"].append([])

    def _boom(open_tasks: Any, *, now: Any = None,
              max_items: int = 2) -> list[dict[str, Any]]:
        raise RuntimeError("stale selector exploded")

    monkeypatch.setattr(_selection_module, "_select_stale_task_nudge", _boom)

    handlers.periodic_summary(cfg, is_rollup=False)

    assert len(harness["sends"]) == 1
    megha_call = harness["claude"].compose_periodic_summary.call_args
    assert megha_call.kwargs["stale_tasks"] == []


def test_nudge_alone_does_not_trigger_max_send(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """Max's all-quiet skip happens BEFORE the nudge (and theme) selection;
    a skipped Max morning pays nothing and gets nothing."""
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])
    harness["stale_result"] = list(_STALE)

    handlers.periodic_summary(cfg, is_rollup=False)

    assert len(harness["stale_calls"]) == 1  # Megha only
    assert len(harness["sends"]) == 1


# ---- deep verify: stale_task_dropped gate + route threading -----------------------


def _cfg() -> dict:
    return {
        "claude": {"model": "claude-sonnet-4-6", "api_key": "fake"},
        "model_routing": {},
    }


class _StubClaudeClient:
    _output: str | None = None
    _seen_kwargs: dict | None = None

    def __init__(self, config: dict) -> None:
        self._config = config

    def compose_periodic_summary(self, **kwargs) -> str | None:
        type(self)._seen_kwargs = kwargs
        return type(self)._output


def _stub_compose(output: str | None):
    _StubClaudeClient._output = output
    _StubClaudeClient._seen_kwargs = None
    return patch(
        "capabilities.kavi_persona.verify.ClaudeClient",
        new=_StubClaudeClient,
    )


def _morning_snapshot(**extra: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "queued_tasks": [],
        "pending_questions": [],
        "pending_facts": [],
        "is_rollup": False,
        "time_of_day": "morning",
        "recipient": "megha",
    }
    snap.update(extra)
    return snap


def test_gate_registered_in_selection_gates() -> None:
    assert "stale_task_dropped" in SELECTION_GATES


def test_gate_fires_when_morning_digest_drops_a_stale_item() -> None:
    with _stub_compose("Quiet morning, nothing new queued."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(), _morning_snapshot(stale_tasks=list(_STALE)),
        )
    assert result["verdict"] == "FAIL"
    fails = [f for f in result["failures"] if f["gate"] == "stale_task_dropped"]
    assert len(fails) == 2, f"expected both items flagged, got {result['failures']}"
    assert "SCT reimbursement ticket" in fails[0]["detail"]


def test_gate_fires_per_item_when_only_one_is_referenced() -> None:
    with _stub_compose(
        "The SCT reimbursement ticket has been open 45 days, "
        "close it out or drop it?"
    ):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(), _morning_snapshot(stale_tasks=list(_STALE)),
        )
    fails = [f for f in result["failures"] if f["gate"] == "stale_task_dropped"]
    assert len(fails) == 1
    assert "library card" in fails[0]["detail"]


def test_gate_passes_when_every_item_is_referenced() -> None:
    with _stub_compose(
        "The SCT reimbursement ticket has been open 45 days, close it out "
        "or drop it? The library card renewal is at 21 days."
    ):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(), _morning_snapshot(stale_tasks=list(_STALE)),
        )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_gate_is_noop_for_rollup() -> None:
    with _stub_compose("3 added today, 1 done, 2 over a week."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            _morning_snapshot(
                is_rollup=True, time_of_day="9pm",
                tasks_added_today_count=3,
                tasks_completed_today_count=1,
                tasks_over_7d_count=2,
                stale_tasks=list(_STALE),
            ),
        )
    assert not any(
        f["gate"] == "stale_task_dropped" for f in result["failures"]
    )


def test_gate_is_noop_when_composer_returns_none() -> None:
    with _stub_compose(None):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(), _morning_snapshot(stale_tasks=list(_STALE)),
        )
    assert result["output"] is None
    assert not any(
        f["gate"] == "stale_task_dropped" for f in result["failures"]
    )


def test_gate_not_armed_without_stale_tasks_in_payload() -> None:
    with _stub_compose("Quiet morning, nothing new queued."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(), _morning_snapshot(),
        )
    assert not any(
        f["gate"] == "stale_task_dropped" for f in result["failures"]
    )


def test_verify_route_threads_and_echoes_stale_tasks() -> None:
    with _stub_compose(
        "The SCT reimbursement ticket has been open 45 days, close it out "
        "or drop it? The library card renewal is at 21 days."
    ):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(), _morning_snapshot(stale_tasks=list(_STALE)),
        )
    kwargs = _StubClaudeClient._seen_kwargs
    assert kwargs is not None
    assert kwargs["stale_tasks"] == _STALE
    assert result["input_payload"]["stale_tasks"] == _STALE


def test_replay_route_threads_and_echoes_stale_tasks_too() -> None:
    with _stub_compose("ok"):
        result = synthetic_compose.replay_periodic_summary(
            _cfg(), _morning_snapshot(stale_tasks=list(_STALE)),
        )
    kwargs = _StubClaudeClient._seen_kwargs
    assert kwargs is not None
    assert kwargs["stale_tasks"] == _STALE
    assert result["input_payload"]["stale_tasks"] == _STALE


def test_owner_leak_scans_stale_task_titles() -> None:
    """A stale item for the OTHER person's task in recipient R's payload
    is an owner leak when the output names it (consistency with due_soon
    and close_suggestions scanning)."""
    with _stub_compose(
        "The cleaner cash payment has waited a while, close it out?"
    ):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            _morning_snapshot(
                stale_tasks=[
                    {"title": "MM Cleaner cash payment", "age_days": 30},
                ],
            ),
        )
    assert any(f["gate"] == "owner_leak" for f in result["failures"])


# ---- registration: the close-or-drop offer must register bindable state -----
#
# Root cause of the 2026-06-26 "keep" -> "I caught myself..." bug: the morning
# stale-task nudge surfaced a close-or-drop offer as narrative text only and
# registered NO pending question, so a later bare "keep" had nothing to bind to,
# degraded to the conversational path, and tripped the G-A1 action-claim gate.
# These tests lock the side-effect the composer-only verify route cannot see.


def _capture_registrations(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _ps_module, "add_pending_question",
        lambda _path, q: captured.append(q),
    )
    return captured


def test_stale_nudge_registers_bindable_pending_question(
    tmp_path: Path, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sent morning digest with a stale-task offer registers one pending
    question per task, carrying the resolvable task_id + VERBATIM title (so
    qa-keep is an idempotent no-op, not a rename) + an expiry."""
    captured = _capture_registrations(monkeypatch)
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [_open_task("t-mj", "MJ Submit SCT reimbursement ticket")]
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])
    harness["theme_result"] = None
    harness["stale_result"] = list(_STALE)

    handlers.periodic_summary(cfg, is_rollup=False)

    # _STALE has two items; registered once each (megha recipient only here).
    sct = [q for q in captured if q["task_id"] == "t-sct"]
    assert len(sct) == 1, f"expected one registration for t-sct, got {captured}"
    q = sct[0]
    assert q["task_id"] == "t-sct"
    # VERBATIM title — the qa-keep executor PATCHes the title to this value.
    assert q["task_title_rendered"] == "MJ Submit SCT reimbursement ticket"
    assert q["kind"] == "q_and_a"
    assert q["expires_at"], "stale-nudge question must carry an expiry"
    assert q["id"].startswith("q-")


def test_stale_nudge_registers_nothing_when_send_fails(
    tmp_path: Path, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unverified, un-fallback'd send must NOT leave a dangling question
    Megha never saw — same invariant as the close-suggestion surfaced-state."""
    captured = _capture_registrations(monkeypatch)

    def _failed_send(*a: Any, **kw: Any) -> dict[str, Any]:
        return {"verified": False, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _failed_send)
    monkeypatch.setattr(_ps_module, "_send_imessage_with_fallback", _failed_send)

    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [_open_task("t-mj", "MJ Submit SCT reimbursement ticket")]
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])
    harness["theme_result"] = None
    harness["stale_result"] = list(_STALE)

    handlers.periodic_summary(cfg, is_rollup=False)

    assert captured == []


def test_drop_expired_questions_lifecycle() -> None:
    """The reply read-path filters expired stale-nudge offers while keeping
    answerable low-conf inbox questions (no expiry) forever."""
    from kavi_runtime.state import drop_expired_questions

    now = datetime(2026, 6, 26, 18, 0, 0, tzinfo=timezone.utc)
    qs = [
        {"id": "low", "task_id": "t1"},                       # no expiry -> kept
        {"id": "live", "expires_at": "2026-06-26T20:00:00Z"}, # future -> kept
        {"id": "dead", "expires_at": "2026-06-26T10:00:00Z"}, # past -> dropped
        {"id": "bad", "expires_at": "garbage"},               # malformed -> dropped
    ]
    kept = [q["id"] for q in drop_expired_questions(qs, now=now)]
    assert kept == ["low", "live"]
