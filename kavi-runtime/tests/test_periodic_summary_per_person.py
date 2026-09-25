"""End-to-end tests for the per-person periodic_summary split (2026-06-10).

One scheduler fire now composes and sends up to TWO messages: Megha's
(her tasks: MJ-prefixed + unprefixed legacy, plus pending Q&A + pending
facts) to megha_phone, Max's (MM-prefixed tasks only) to max_phone.

Contract locked here:

  * One fire loops both recipients with the correct handles; Max's send
    names his handle explicitly and suppresses the Outlook fallback.
  * Counts and due-soon items are computed per person.
  * Max receives a message ONLY when his selection is non-empty
    (engineering call, flagged for PM review in the spec changelog).
  * Megha always receives hers, including the all-clear shapes.
  * Pending facts / pending questions stay in Megha's compose only.
  * Debounce hash state is keyed per recipient; the legacy single-entry
    state file migrates as Megha's.
  * Summary anchors are saved under each recipient's own handle.
  * The morning digest fires for due-soon items even when everything
    else is quiet, and repeats across days (days_until feeds the hash).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_periodic_summary_per_person.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from capabilities.kavi_persona.composers import periodic_summary as _ps_module

MEGHA_PHONE = "+15555550101"
MAX_PHONE = "+15555550102"

_TODAY_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT08:00:00Z")


def _base_config(tmp_path: Path, *, max_phone: str = MAX_PHONE) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    evals_dir = tmp_path / "evals" / "kavi-persona"
    evals_dir.mkdir(parents=True, exist_ok=True)
    imessage = {"megha_phone": MEGHA_PHONE}
    if max_phone:
        imessage["max_phone"] = max_phone
    return {
        "imessage": imessage,
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
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub graph / LLM / send / queue state. Returns a registry the test
    mutates before firing handlers.periodic_summary.

    `sends` records (text, recipient_handle, suppress_outlook_fallback)
    per send-wrapper call. `open_tasks` / `completed_tasks` drive the
    graph stub; `queued_seq` / `pending_seq` drive the state drains.
    """
    reg: dict[str, Any] = {
        "sends": [],
        "anchors": [],
        "open_tasks": [],
        "completed_tasks": [],
        "queued_seq": [],
        "pending_seq": [],
    }

    def _send(
        config: dict, text: str, *, kind: str,
        recipient_handle: str | None = None,
        suppress_outlook_fallback: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        reg["sends"].append({
            "text": text,
            "kind": kind,
            "recipient_handle": recipient_handle,
            "suppress_outlook_fallback": suppress_outlook_fallback,
        })
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send)
    monkeypatch.setattr(_ps_module, "_send_imessage_with_fallback", _send)

    def _save_anchor(path, recipient_handle, **kwargs):
        reg["anchors"].append({"recipient_handle": recipient_handle, **kwargs})

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

    return reg


def _open_task(task_id: str, title: str, *, created: str = _TODAY_ISO, due: str | None = None):
    t = {"id": task_id, "title": title, "createdDateTime": created}
    if due:
        t["dueDateTime"] = {"dateTime": f"{due}T00:00:00.0000000", "timeZone": "UTC"}
    return t


# ---- one fire → two recipients, correct handles ------------------------------


def test_one_fire_sends_to_both_recipients_with_correct_handles(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Book dentist"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Book dentist"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    sends = harness["sends"]
    assert len(sends) == 2, f"expected one send per recipient, got {sends}"
    megha_send, max_send = sends
    # Megha's send keeps the legacy default-recipient call shape.
    assert megha_send["recipient_handle"] is None
    assert megha_send["suppress_outlook_fallback"] is False
    # Max's send names his handle and forbids the Outlook fallback.
    assert max_send["recipient_handle"] == MAX_PHONE
    assert max_send["suppress_outlook_fallback"] is True


def test_each_recipient_composed_with_own_tasks_and_name(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Book dentist"),
        _open_task("t-legacy", "Order diapers"),  # unprefixed → Megha
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Book dentist"},
        {"task_id": "t-legacy", "title": "Order diapers"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    calls = harness["claude"].compose_periodic_summary.call_args_list
    assert len(calls) == 2
    megha_call, max_call = calls
    megha_titles = [t["title"] for t in megha_call.args[0]]
    max_titles = [t["title"] for t in max_call.args[0]]
    assert megha_titles == ["MJ Book dentist", "Order diapers"]
    assert max_titles == ["MM Leave cleaner cash"]
    assert megha_call.kwargs["recipient_name"] == "Megha"
    assert max_call.kwargs["recipient_name"] == "Max"


def test_counts_are_computed_per_person(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """10-added-but-4-are-Megha's: her rollup says 4, his says 6 — never
    the household total."""
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = (
        [_open_task(f"mj-{i}", f"MJ Task {i}") for i in range(4)]
        + [_open_task(f"mm-{i}", f"MM Task {i}") for i in range(6)]
    )
    harness["queued_seq"].append([
        {"task_id": "mj-0", "title": "MJ Task 0"},
        {"task_id": "mm-0", "title": "MM Task 0"},
    ])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    megha_call, max_call = harness["claude"].compose_periodic_summary.call_args_list
    assert megha_call.kwargs["tasks_added_today_count"] == 4
    assert max_call.kwargs["tasks_added_today_count"] == 6


# ---- Max skip-empty / Megha always-sends -------------------------------------


def test_max_send_suppressed_when_his_selection_is_empty(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """All-quiet for Max (no MM tasks anywhere) → nothing sent to him,
    no compose for him."""
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [_open_task("t-mj", "MJ Book dentist")]
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Book dentist"}])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    sends = harness["sends"]
    assert len(sends) == 1
    assert sends[0]["recipient_handle"] is None  # Megha default
    harness["claude"].compose_periodic_summary.assert_called_once()


def test_megha_all_clear_morning_still_sends(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """Everything empty at 7 AM → Megha still gets her all-clear; Max
    gets nothing."""
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=False)

    sends = harness["sends"]
    assert len(sends) == 1, "Megha always receives hers, including all-clear"
    assert sends[0]["recipient_handle"] is None
    harness["claude"].compose_periodic_summary.assert_called_once()
    assert (
        harness["claude"].compose_periodic_summary.call_args.kwargs["recipient_name"]
        == "Megha"
    )


def test_megha_all_clear_rollup_still_sends(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    assert len(harness["sends"]) == 1
    assert harness["sends"][0]["recipient_handle"] is None


def test_max_due_soon_item_alone_triggers_his_morning_send(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """≥1 due-soon item counts as non-empty selection for Max even with
    nothing queued and zero counts."""
    cfg = _base_config(tmp_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    harness["open_tasks"] = [
        # Created long ago (not "added today", not "over 7d" matters not),
        # but due today → enters the runway.
        _open_task("t-mm", "MM Leave cleaner cash",
                   created=datetime.now(timezone.utc).strftime("%Y-%m-%dT00:30:00Z"),
                   due=today),
    ]
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=False)

    max_sends = [s for s in harness["sends"] if s["recipient_handle"] == MAX_PHONE]
    assert len(max_sends) == 1
    max_call = [
        c for c in harness["claude"].compose_periodic_summary.call_args_list
        if c.kwargs["recipient_name"] == "Max"
    ]
    assert len(max_call) == 1
    assert max_call[0].kwargs["due_soon"][0]["title"] == "MM Leave cleaner cash"


def test_max_skipped_when_max_phone_not_configured(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path, max_phone="")
    harness["open_tasks"] = [_open_task("t-mm", "MM Leave cleaner cash")]
    harness["queued_seq"].append([{"task_id": "t-mm", "title": "MM Leave cleaner cash"}])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    assert all(s["recipient_handle"] is None for s in harness["sends"])


# ---- pending facts / questions stay Megha-only -------------------------------


def test_pending_questions_and_facts_stay_in_meghas_compose_only(
    tmp_path: Path, harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Book dentist"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Book dentist"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([
        {"task_id": "t-mj", "task_title_rendered": "MJ Book dentist"},
    ])
    # One fresh pending fact on disk.
    Path(cfg["paths"]["pending_facts"]).write_text(json.dumps({
        "pending_id": "p1", "ts": "2026-06-10T01:00:00Z",
        "fact_text": "Nadia needs beans soaked", "scope": "household",
        "source_decision_id": None, "expires_at": None,
        "status": "pending_confirmation", "inbound_source": "imessage from Nadia",
        "todo": "",
    }) + "\n")

    handlers.periodic_summary(cfg, is_rollup=True)

    megha_call, max_call = harness["claude"].compose_periodic_summary.call_args_list
    assert megha_call.kwargs["recipient_name"] == "Megha"
    assert len(megha_call.args[1]) == 1  # pending_questions
    assert len(megha_call.kwargs["pending_facts"]) == 1
    assert max_call.kwargs["recipient_name"] == "Max"
    assert max_call.args[1] == []  # no pending questions for Max
    assert max_call.kwargs["pending_facts"] == []


# ---- per-recipient anchors ----------------------------------------------------


def test_anchor_saved_under_each_recipients_own_handle(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Book dentist"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Book dentist"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    anchors = harness["anchors"]
    by_handle = {a["recipient_handle"]: a for a in anchors}
    assert set(by_handle) == {MEGHA_PHONE, MAX_PHONE}
    assert by_handle[MEGHA_PHONE]["anchor_task_id"] == "t-mj"
    assert by_handle[MAX_PHONE]["anchor_task_id"] == "t-mm"


# ---- per-recipient debounce state + legacy migration --------------------------


def _hash_file(cfg: dict) -> Path:
    return Path(cfg["paths"]["imessage_state"]).parent / "periodic_summary_last_hash.json"


def test_debounce_state_keyed_per_recipient(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """After a both-recipients fire, Megha's hash sits at the legacy
    top level and Max's under the recipients sub-dict."""
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Book dentist"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Book dentist"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    data = json.loads(_hash_file(cfg).read_text())
    assert data.get("hash"), "Megha's entry lives at the legacy top level"
    assert data["recipients"]["max"]["hash"], "Max's entry nests under recipients"
    assert data["hash"] != data["recipients"]["max"]["hash"]


def test_legacy_hash_file_migrates_as_meghas(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """A pre-split state file (single top-level {hash, ts, kind}) is read
    as Megha's debounce entry: an identical Megha input suppresses her
    send while Max's (keyed separately) still fires."""
    cfg = _base_config(tmp_path)
    megha_queued = [{"task_id": "t-mj", "title": "MJ Book dentist"}]
    legacy_hash = _ps_module._periodic_summary_input_hash(
        megha_queued, [], [], due_soon=[],
    )
    _hash_file(cfg).parent.mkdir(parents=True, exist_ok=True)
    _hash_file(cfg).write_text(json.dumps({
        "hash": legacy_hash,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "rollup",
    }))

    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Book dentist"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Book dentist"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    sends = harness["sends"]
    assert len(sends) == 1, "Megha suppressed by migrated legacy hash; Max sends"
    assert sends[0]["recipient_handle"] == MAX_PHONE


def test_due_soon_repeats_next_day_despite_debounce(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """The debounce hash includes days_until, which decrements daily — so
    a due-soon morning digest is never swallowed as yesterday's duplicate."""
    a = _ps_module._periodic_summary_input_hash(
        [], [], [], due_soon=[{"title": "MJ Pay Boonli", "days_until": 2}],
    )
    b = _ps_module._periodic_summary_input_hash(
        [], [], [], due_soon=[{"title": "MJ Pay Boonli", "days_until": 1}],
    )
    assert a != b


def test_rollup_compose_receives_no_due_soon(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """Deadline runway is a morning-digest shape; the 9 PM rollup keeps
    counts + named-top."""
    cfg = _base_config(tmp_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    harness["open_tasks"] = [_open_task("t-mj", "MJ Pay Boonli", due=today)]
    harness["queued_seq"].append([{"task_id": "t-mj", "title": "MJ Pay Boonli"}])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    megha_call = harness["claude"].compose_periodic_summary.call_args_list[0]
    assert megha_call.kwargs["due_soon"] == []


def test_megha_morning_fires_for_due_soon_alone(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    """An overdue-but-open task keeps Megha's morning digest firing even
    when nothing else is queued."""
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Submit BCBA paperwork", due="2026-06-01"),
    ]
    harness["queued_seq"].append([])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=False)

    assert len(harness["sends"]) == 1
    call = harness["claude"].compose_periodic_summary.call_args
    assert call.kwargs["due_soon"][0]["title"] == "MJ Submit BCBA paperwork"
    assert call.kwargs["due_soon"][0]["days_until"] < 0


# ---- composer marshaling -------------------------------------------------------


def _fake_compose_client() -> MagicMock:
    """Same client double as tests/test_pending_facts_full_text_selection.py:
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


def test_composer_marshals_due_soon_and_recipient_into_llm_input() -> None:
    from capabilities.kavi_persona.composers.periodic_summary import (
        compose_periodic_summary,
    )

    client = _fake_compose_client()
    compose_periodic_summary(
        client,
        queued_tasks=[], pending_questions=[],
        is_rollup=False, time_of_day="morning",
        due_soon=[{"title": "MM Leave cleaner cash", "due_date": "2026-06-10",
                   "due_weekday": "Wednesday", "days_until": 0}],
        recipient_name="Max",
    )
    user_msg = client._anthropic.messages.create.call_args.kwargs["messages"][0]["content"]
    payload = json.loads(user_msg.split("<input>\n", 1)[1].split("\n</input>")[0])
    assert payload["recipient"] == "Max"
    assert payload["due_soon"] == [
        {"title": "MM Leave cleaner cash", "due_date": "2026-06-10",
         "due_weekday": "Wednesday", "days_until": 0},
    ]
    assert "iMessage to Max" in user_msg


def test_composer_defaults_preserve_pre_split_contract() -> None:
    """Callers that don't pass the new kwargs get Megha + empty due_soon
    — back-compat with every pre-2026-06-10 call site."""
    from capabilities.kavi_persona.composers.periodic_summary import (
        compose_periodic_summary,
    )

    client = _fake_compose_client()
    compose_periodic_summary(
        client,
        queued_tasks=[], pending_questions=[],
        is_rollup=True, time_of_day="9pm",
    )
    user_msg = client._anthropic.messages.create.call_args.kwargs["messages"][0]["content"]
    payload = json.loads(user_msg.split("<input>\n", 1)[1].split("\n</input>")[0])
    assert payload["recipient"] == "Megha"
    assert payload["due_soon"] == []
    assert "iMessage to Megha" in user_msg


# ---- eval rows carry the recipient -------------------------------------------


def test_runs_rows_carry_recipient_field(
    tmp_path: Path, harness: dict[str, Any],
) -> None:
    cfg = _base_config(tmp_path)
    harness["open_tasks"] = [
        _open_task("t-mj", "MJ Book dentist"),
        _open_task("t-mm", "MM Leave cleaner cash"),
    ]
    harness["queued_seq"].append([
        {"task_id": "t-mj", "title": "MJ Book dentist"},
        {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
    ])
    harness["pending_seq"].append([])

    handlers.periodic_summary(cfg, is_rollup=True)

    rows = [
        json.loads(line)
        for line in Path(cfg["paths"]["runtime_events_jsonl"]).read_text().splitlines()
        if line.strip()
    ]
    recipients = [r["recipient"] for r in rows if r["event_type"] == "periodic_summary"]
    assert recipients == ["megha", "max"]
