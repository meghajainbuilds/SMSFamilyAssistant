"""Webhook re-fire dedup across every email-decision path.

Why this exists: 2026-05-27. MS Graph re-fires the email-arrival webhook
roughly twice per email (median gap 15 sec). Pre-fix, `_dedup_record` was
only called from the task-create + lifecycle-update paths. When Kavi
decided to SKIP an email (the majority of inbox traffic), the dedup cache
never recorded the message_id, so the next webhook re-fire paid the full
LLM cost from scratch.

This week's evidence: 151 decisions on 82 unique emails; 68 of 82 emails
processed twice. Estimated waste: ~$3.40/week / ~$180/year.

The fix records the decision outcome on EVERY path — skip, create,
dedup_hit, updated, paused, token_blowup, stale, non-inbox, own-outbound —
and adds an early `_dedup_check` at the top of `_email_arrived_impl` so a
re-fire returns immediately with a `webhook_redup_hit` row appended to
eval-inbox-judgments.jsonl. The household sees no change in behavior
(decisions were already consistent across the dupe pairs); the waste
stops.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_webhook_redup_dedup.py -v
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.runtime import dedup


# ---- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dedup_cache() -> None:
    """Each test starts with an empty dedup cache so tests don't leak state
    into each other. The cache is a module-level global on handlers."""
    dedup._dedup_cache.clear()
    dedup._message_locks.clear()
    yield
    dedup._dedup_cache.clear()
    dedup._message_locks.clear()


def _base_config(tmp_path: Path) -> dict[str, Any]:
    """Minimal config wiring all the paths _email_arrived_impl touches."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    evals_dir = tmp_path / "evals" / "inbox-to-task"
    evals_dir.mkdir(parents=True)
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {"mstodo_shared_list_id": "AQTEST=="},
        "schedule": {
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
        },
        "paths": {
            "imessage_state": str(state_dir / "imessage-state.json"),
            "runs_jsonl": str(tmp_path / "runs.jsonl"),
            "runtime_events_jsonl": str(tmp_path / "runtime-events.jsonl"),
            "eval_inbox_judgments_jsonl": str(
                evals_dir / "eval-inbox-judgments.jsonl"
            ),
            "corrections_jsonl": str(tmp_path / "corrections.jsonl"),
            "promoted_patterns_jsonl": str(tmp_path / "promoted_patterns.jsonl"),
        },
        "priority_senders": [],
    }


def _read_eval_rows(config: dict) -> list[dict[str, Any]]:
    path = Path(config["paths"]["eval_inbox_judgments_jsonl"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _notification(message_id: str) -> dict[str, Any]:
    return {
        "resource": f"/me/messages/{message_id}",
        "resourceData": {"id": message_id},
    }


def _stub_msg(message_id: str, *, from_addr: str = "school@example.com",
              subject: str = "Newsletter") -> dict[str, Any]:
    """Build a Graph message payload the way fetch_message would return one."""
    return {
        "id": message_id,
        "subject": subject,
        "from": {"emailAddress": {"name": "School", "address": from_addr}},
        "toRecipients": [{"emailAddress": {"address": "megha@example.com"}}],
        "receivedDateTime": "2026-05-27T10:00:00Z",
        "body": {"content": "Hi parents — this week's newsletter is attached."},
        "bodyPreview": "Hi parents — this week's newsletter is attached.",
        "parentFolderId": "inbox-folder-1",
        "conversationId": None,
    }


@pytest.fixture
def stub_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub GraphClient, ClaudeClient, BlueBubblesClient. Tests control the
    LLM verdict via `claude_result` and the existing-task lookup via
    `existing_task`. The Graph client always reports the message as living
    in the inbox so the non-inbox short-circuit doesn't fire by default."""
    graph = MagicMock()
    claude = MagicMock()
    bb = MagicMock()

    state = {
        "claude_result": {"status": "skipped", "reason": "marketing newsletter"},
        "existing_task": None,
        "create_task_id": "task-fresh-001",
    }

    graph.inbox_folder_id.return_value = "inbox-folder-1"
    graph.fetch_thread.return_value = []
    graph.fetch_sentitems_replies.return_value = []
    graph.resolve_shared_list_id.return_value = "AQTEST=="
    graph.find_todo_task_by_source_email.side_effect = (
        lambda *a, **kw: state["existing_task"]
    )
    graph.find_todo_task_by_package_id.return_value = None
    graph.find_todo_task_by_heuristic.return_value = (None, "")
    graph.list_open_todo_tasks.return_value = []
    graph.create_todo_task.side_effect = (
        lambda *a, **kw: state["create_task_id"]
    )
    claude.run_email_to_tasks.side_effect = lambda *a, **kw: state["claude_result"]
    claude.check_semantic_duplicate.return_value = {"is_duplicate": False}

    _stub_get_clients = lambda c: (graph, claude, bb)

    monkeypatch.setattr(handlers, "_get_clients", _stub_get_clients)

    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod

    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_get_clients)

    monkeypatch.setattr(_send_mod, "_get_clients", _stub_get_clients)
    # Stub fetch_message so default tests don't have to set it per-test.
    def _fetch(message_id: str, *, account: str | None = None) -> dict[str, Any]:
        return _stub_msg(message_id)

    graph.fetch_message.side_effect = _fetch

    # Suppress iMessage sends so we don't go near BlueBubbles.
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback",
        lambda *a, **kw: {"verified": True, "fallback_used": False,
                          "sent": True, "blocked": False},
    )

    # Bypass spend cap check so it doesn't read a non-existent runs path.
    monkeypatch.setattr(handlers, "_check_spend_cap_after_call",
                        lambda c, sp: None)

    return {"graph": graph, "claude": claude, "bb": bb, "state": state}


# ---- helper-level: cache shape + TTL ---------------------------------------


def test_dedup_cache_returns_dict_outcome() -> None:
    """The cache stores the full decision outcome, not just task_id."""
    msg_id = "msg-cache-shape-001"
    handlers._dedup_record(msg_id, {
        "decision": "skipped", "task_id": None, "decision_id": "d_abc",
    })
    cached = handlers._dedup_check(msg_id)
    assert cached is not None
    assert cached["decision"] == "skipped"
    assert cached["task_id"] is None
    assert cached["decision_id"] == "d_abc"


def test_dedup_cache_accepts_legacy_string_signature() -> None:
    """Out-of-tree callers using the pre-2026-05-27 string signature must
    continue to work. The string is interpreted as a bare task_id."""
    msg_id = "msg-legacy-shape-001"
    handlers._dedup_record(msg_id, "AAMkADAaa==")  # legacy string form
    cached = handlers._dedup_check(msg_id)
    assert cached is not None
    assert cached["task_id"] == "AAMkADAaa=="
    assert cached["decision"] == "unknown"


# ---- acceptance criterion: record on every decision path -------------------


def test_dedup_records_on_skip_path(
    tmp_path: Path, stub_clients: dict[str, Any],
) -> None:
    """When Kavi judges an email as skip-worthy, the message_id must land in
    the dedup cache so the next webhook re-fire short-circuits."""
    cfg = _base_config(tmp_path)
    stub_clients["state"]["claude_result"] = {
        "status": "skipped", "reason": "marketing newsletter",
    }
    msg_id = "msg-skip-001"

    # First arrival: full path runs, records dedup.
    result = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result["result"]["status"] == "skipped"
    assert handlers._dedup_check(msg_id) is not None
    cached = handlers._dedup_check(msg_id)
    assert cached["decision"] == "skipped"

    # Second arrival within TTL: should short-circuit with webhook_redup_hit
    # and NOT call run_email_to_tasks a second time.
    call_count_before = stub_clients["claude"].run_email_to_tasks.call_count
    result2 = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result2["status"] == "webhook_redup_hit"
    assert result2["suppressed"] is True
    # No additional LLM call.
    assert stub_clients["claude"].run_email_to_tasks.call_count == call_count_before


def test_dedup_records_on_create_path(
    tmp_path: Path, stub_clients: dict[str, Any],
) -> None:
    """A successful task-create must land the task_id in the dedup cache.
    (Regression cover for the original 2026-05-05 dedup wiring.)"""
    cfg = _base_config(tmp_path)
    stub_clients["state"]["claude_result"] = {
        "status": "task",
        "task": {
            "title": "Pick up library book",
            "owner": "megha",
            "confidence": "high",
            "source_tag": "school",
            "owner_reason": "library hold expires Friday",
        },
        "_usage": {"input_tokens": 100, "output_tokens": 20},
    }
    msg_id = "msg-create-001"

    result = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result["result"]["status"] == "task"
    cached = handlers._dedup_check(msg_id)
    assert cached is not None
    assert cached["decision"] == "created"
    assert cached["task_id"] == "task-fresh-001"

    # Second arrival short-circuits.
    create_calls_before = stub_clients["graph"].create_todo_task.call_count
    result2 = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result2["status"] == "webhook_redup_hit"
    assert stub_clients["graph"].create_todo_task.call_count == create_calls_before


def test_dedup_records_on_dedup_hit_path(
    tmp_path: Path, stub_clients: dict[str, Any],
) -> None:
    """When the second-line-of-defense Graph dedup catches an existing task,
    the message_id must land in the dedup cache too."""
    cfg = _base_config(tmp_path)
    stub_clients["state"]["claude_result"] = {
        "status": "task",
        "task": {
            "title": "Pay tuition",
            "owner": "megha",
            "confidence": "high",
            "source_tag": "school",
            "owner_reason": "tuition due",
        },
        "_usage": {"input_tokens": 100, "output_tokens": 20},
    }
    stub_clients["state"]["existing_task"] = "task-already-exists-xyz"
    msg_id = "msg-graph-dedup-001"

    result = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result.get("dedup") == "graph"
    cached = handlers._dedup_check(msg_id)
    assert cached is not None
    assert cached["decision"] == "dedup_hit"
    assert cached["task_id"] == "task-already-exists-xyz"


def test_dedup_records_on_updated_path(
    tmp_path: Path, stub_clients: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Package lifecycle UPDATE on an existing task must also record dedup
    so a re-fired Shipped/OFD/Delivered webhook short-circuits and avoids
    a duplicate iMessage ping."""
    cfg = _base_config(tmp_path)
    msg_id = "msg-lifecycle-update-001"

    # Configure the message to look like a (non-Amazon) Chewy OFD update so
    # the package_extractor returns a hint and Tier 1 finds an existing task.
    # Amazon/Whole-Foods shipment emails are skipped wholesale now, so a
    # regular UPS-tracked store exercises the updated-path dedup.
    def _fetch(message_id: str, *, account: str | None = None) -> dict[str, Any]:
        m = _stub_msg(message_id,
                      from_addr="order-update@chewy.com",
                      subject="Your Chewy order is out for delivery")
        m["body"]["content"] = (
            "Tracking 1Z999AA10123456784. Out for delivery now. ETA: 8:30-9:30am."
        )
        return m
    stub_clients["graph"].fetch_message.side_effect = _fetch
    stub_clients["graph"].find_todo_task_by_package_id.return_value = "task-pkg-001"
    stub_clients["graph"].get_todo_task.return_value = {
        "title": "MJ Chewy order",
        "body": {"content": "audit trail line 1"},
    }
    stub_clients["graph"].update_todo_task.return_value = None
    # is_quiet_hours uses datetime.now — patch it directly so the OFD
    # iMessage path doesn't try to actually send anything we care about.
    monkeypatch.setattr(handlers, "is_quiet_hours", lambda c: True)

    result = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result.get("task_id") == "task-pkg-001"
    cached = handlers._dedup_check(msg_id)
    assert cached is not None
    assert cached["decision"] == "updated"
    assert cached["task_id"] == "task-pkg-001"

    # Second arrival short-circuits — no second iMessage, no second PATCH.
    update_calls_before = stub_clients["graph"].update_todo_task.call_count
    result2 = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result2["status"] == "webhook_redup_hit"
    assert stub_clients["graph"].update_todo_task.call_count == update_calls_before


# ---- TTL expiry ------------------------------------------------------------


def test_dedup_expires_after_ttl(
    tmp_path: Path, stub_clients: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After _DEDUP_TTL_SEC elapses, the same message_id MUST go through
    fresh — the cache is a short-window suppressor, not a permanent dedup."""
    cfg = _base_config(tmp_path)
    stub_clients["state"]["claude_result"] = {
        "status": "skipped", "reason": "marketing",
    }
    msg_id = "msg-ttl-001"

    # Arrival 1: records dedup.
    handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert handlers._dedup_check(msg_id) is not None

    # Forge time to be _DEDUP_TTL_SEC + 1 sec later by mutating the cache
    # entry's stored ts in-place (cleaner than monkeypatching time.monotonic
    # everywhere _dedup_check reads it).
    entry = dedup._dedup_cache[msg_id]
    aged_ts = entry[1] - (dedup._DEDUP_TTL_SEC + 1)
    dedup._dedup_cache[msg_id] = (entry[0], aged_ts)

    # _dedup_check prunes stale entries on lookup.
    assert handlers._dedup_check(msg_id) is None

    # Second arrival now goes through fresh.
    call_count_before = stub_clients["claude"].run_email_to_tasks.call_count
    result2 = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result2["result"]["status"] == "skipped"
    assert stub_clients["claude"].run_email_to_tasks.call_count == call_count_before + 1


# ---- eval log row shape ----------------------------------------------------


def test_webhook_redup_hit_logged_to_eval(
    tmp_path: Path, stub_clients: dict[str, Any],
) -> None:
    """A re-fire must append a row to eval-inbox-judgments.jsonl with
    decision='webhook_redup_hit', suppressed=true, and a reference back
    to the prior decision_id so /eval-inbox-week can pair them up."""
    cfg = _base_config(tmp_path)
    stub_clients["state"]["claude_result"] = {
        "status": "skipped", "reason": "marketing newsletter",
    }
    msg_id = "msg-eval-row-001"

    # Arrival 1: skipped path. Writes ONE row (decision=skipped).
    handlers._email_arrived_impl(_notification(msg_id), cfg)
    rows_after_1 = _read_eval_rows(cfg)
    assert len(rows_after_1) == 1
    assert rows_after_1[0]["decision"] == "skipped"
    original_decision_id = rows_after_1[0]["decision_id"]

    # Arrival 2: webhook re-fire. Writes ONE additional row with decision=
    # webhook_redup_hit + suppressed=true.
    handlers._email_arrived_impl(_notification(msg_id), cfg)
    rows_after_2 = _read_eval_rows(cfg)
    assert len(rows_after_2) == 2
    redup_row = rows_after_2[1]
    assert redup_row["decision"] == "webhook_redup_hit"
    assert redup_row["email_id"] == msg_id
    extras = redup_row.get("extras") or {}
    assert extras.get("suppressed") is True
    # The prior_decision field carries the original decision; the prior
    # decision row's decision_id was recorded too (None in the skip path
    # today, but the field exists).
    assert extras.get("prior_decision") == "skipped"
    # The original decision_id must still be readable from arrival 1's row.
    assert original_decision_id is not None


# ---- 2026-09-23: in-flight claim stops the concurrent double judgment -------


def test_concurrent_refire_is_judged_once(monkeypatch, tmp_path) -> None:
    """Two deliveries of the same message 1s apart while the first judgment is
    still running: exactly one fetch + judgment; the second is an in-flight
    redup hit. Repro from the 2026-09-23 investigation (~25% of judgments)."""
    import threading
    import time as _time
    from unittest.mock import MagicMock
    import kavi_runtime.handlers as h_mod
    from capabilities.inbox_to_task import handler

    fetches: list[str] = []

    def slow_fetch(mid, account=None):
        fetches.append(mid)
        _time.sleep(0.5)  # judgment still in flight when the re-fire lands
        return None  # stale path: ends processing cleanly after the fetch

    graph = MagicMock()
    graph.fetch_message.side_effect = slow_fetch
    monkeypatch.setattr(h_mod, "_get_clients", lambda c: (graph, MagicMock(), MagicMock()))
    monkeypatch.setattr(h_mod, "is_paused", lambda sp: False)
    monkeypatch.setattr(h_mod, "_dedup_check", lambda mid: None)
    monkeypatch.setattr(h_mod, "_dedup_record", lambda mid, rec: None)
    monkeypatch.setattr(handler, "_log_webhook_redup_hit", lambda *a, **k: None)
    cfg = {"paths": {"imessage_state": str(tmp_path / "s.json")}}
    notif = {"resourceData": {"id": "TEST-DUP-001"}}

    results: list[dict] = []
    t = threading.Thread(target=lambda: results.append(handler._email_arrived_impl(notif, cfg)))
    t.start()
    _time.sleep(0.1)
    second = handler._email_arrived_impl(notif, cfg)
    t.join()

    assert fetches == ["TEST-DUP-001"]
    assert second["status"] == "webhook_redup_hit"
    assert second["prior_decision"] == "in_flight"
    # Claim released: a later genuine retry is processed, not suppressed.
    handler._email_arrived_impl(notif, cfg)
    assert fetches == ["TEST-DUP-001", "TEST-DUP-001"]


def test_claim_released_when_processing_raises(monkeypatch, tmp_path) -> None:
    from unittest.mock import MagicMock
    import pytest as _pytest
    import kavi_runtime.handlers as h_mod
    from capabilities.inbox_to_task import handler
    from kavi_runtime.runtime.dedup import _claim_in_flight, _release_in_flight

    graph = MagicMock()
    graph.fetch_message.side_effect = RuntimeError("graph down")
    monkeypatch.setattr(h_mod, "_get_clients", lambda c: (graph, MagicMock(), MagicMock()))
    monkeypatch.setattr(h_mod, "is_paused", lambda sp: False)
    monkeypatch.setattr(h_mod, "_dedup_check", lambda mid: None)
    cfg = {"paths": {"imessage_state": str(tmp_path / "s.json")}}
    with _pytest.raises(RuntimeError):
        handler._email_arrived_impl({"resourceData": {"id": "CRASH-1"}}, cfg)
    assert _claim_in_flight("CRASH-1") is True  # claim was released
    _release_in_flight("CRASH-1")
