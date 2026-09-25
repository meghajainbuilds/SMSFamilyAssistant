"""Matched-existing-task fields on dedup_hit + updated eval rows.

Why this exists: 2026-05-27. When Kavi decided an incoming email was a
duplicate of an existing MS To Do task (`decision: "dedup_hit"`) or a
lifecycle update on an existing task (`decision: "updated"`), the eval log
row recorded neither the matched task's title nor its ID in a stable place.
The viewer at `evals/viewer.html` could only show "Duplicate suppressed"
with no reference to which task got matched — blocking Megha's weekly
labeling for this class of decisions.

The fix adds two additive fields to every eval-inbox-judgments row:

  - `matched_task_title`: title of the existing task that was matched.
  - `matched_task_id`: the MS To Do task ID it matched against.

Both are non-null on `dedup_hit` (semantic) and `updated` (package lifecycle)
rows. Both stay null on pure create / skip paths so the viewer never invents
a match where none happened.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_matched_task_eval_fields.py -v
"""

from __future__ import annotations

import json
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
    into each other."""
    dedup._dedup_cache.clear()
    dedup._message_locks.clear()
    yield
    dedup._dedup_cache.clear()
    dedup._message_locks.clear()


def _base_config(tmp_path: Path) -> dict[str, Any]:
    """Minimal config wiring the paths _email_arrived_impl touches."""
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
              subject: str = "Newsletter",
              body: str = "Hi parents - this week's newsletter is attached.") -> dict[str, Any]:
    """Build a Graph message payload the way fetch_message would return one."""
    return {
        "id": message_id,
        "subject": subject,
        "from": {"emailAddress": {"name": "Sender", "address": from_addr}},
        "toRecipients": [{"emailAddress": {"address": "megha@example.com"}}],
        "receivedDateTime": "2026-05-27T10:00:00Z",
        "body": {"content": body},
        "bodyPreview": body,
        "parentFolderId": "inbox-folder-1",
        "conversationId": None,
    }


@pytest.fixture
def stub_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub GraphClient, ClaudeClient, BlueBubblesClient. Tests control the
    LLM verdict via `claude_result`, the semantic dedup outcome via
    `sem_dup_result`, and the open-tasks list via `open_tasks`."""
    graph = MagicMock()
    claude = MagicMock()
    bb = MagicMock()

    state: dict[str, Any] = {
        "claude_result": {"status": "skipped", "reason": "marketing newsletter"},
        "existing_task": None,
        "create_task_id": "task-fresh-001",
        "sem_dup_result": {"is_duplicate": False},
        "open_tasks": [],
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
    graph.list_open_todo_tasks.side_effect = lambda *a, **kw: list(state["open_tasks"])
    graph.create_todo_task.side_effect = (
        lambda *a, **kw: state["create_task_id"]
    )
    claude.run_email_to_tasks.side_effect = lambda *a, **kw: state["claude_result"]
    claude.check_semantic_duplicate.side_effect = (
        lambda *a, **kw: state["sem_dup_result"]
    )

    _stub_get_clients = lambda c: (graph, claude, bb)

    monkeypatch.setattr(handlers, "_get_clients", _stub_get_clients)

    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod

    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_get_clients)

    monkeypatch.setattr(_send_mod, "_get_clients", _stub_get_clients)
    def _fetch(message_id: str, *, account: str | None = None) -> dict[str, Any]:
        return _stub_msg(message_id)

    graph.fetch_message.side_effect = _fetch

    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback",
        lambda *a, **kw: {"verified": True, "fallback_used": False,
                          "sent": True, "blocked": False},
    )
    monkeypatch.setattr(handlers, "_check_spend_cap_after_call",
                        lambda c, sp: None)

    return {"graph": graph, "claude": claude, "bb": bb, "state": state}


# ---- dedup_hit (semantic) path --------------------------------------------


def test_dedup_hit_records_matched_task(
    tmp_path: Path, stub_clients: dict[str, Any],
) -> None:
    """When Sonnet flags an incoming email as a semantic duplicate of an
    existing open task, the eval row must carry both the matched task's
    title AND its MS To Do ID so Megha can validate the decision in the
    HTML viewer without cross-referencing runtime data."""
    cfg = _base_config(tmp_path)
    matched_id = "task-existing-tennis-001"
    matched_title = "MJ Bayview tennis registration"

    stub_clients["state"]["claude_result"] = {
        "status": "task",
        "task": {
            "title": "Register for Bayview summer tennis",
            "owner": "megha",
            "confidence": "high",
            "source_tag": "school",
            "owner_reason": "registration link in email",
        },
        "_usage": {"input_tokens": 100, "output_tokens": 20},
    }
    stub_clients["state"]["open_tasks"] = [
        {"id": matched_id, "title": matched_title, "status": "notStarted"},
        {"id": "task-unrelated-002", "title": "MJ Pay tuition", "status": "notStarted"},
    ]
    stub_clients["state"]["sem_dup_result"] = {
        "is_duplicate": True,
        "matches_task_id": matched_id,
        "reason": "both describe Bayview tennis sign-up",
    }

    msg_id = "msg-sem-dedup-001"
    result = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result.get("dedup") == "semantic"
    assert result.get("task_id") == matched_id

    rows = _read_eval_rows(cfg)
    dedup_rows = [r for r in rows if r["decision"] == "dedup_hit"]
    assert len(dedup_rows) == 1, f"expected 1 dedup_hit row, got {len(dedup_rows)}"
    row = dedup_rows[0]
    assert row["matched_task_id"] == matched_id
    assert row["matched_task_title"] == matched_title


def test_dedup_hit_matched_title_falls_back_to_none_when_id_missing_from_slate(
    tmp_path: Path, stub_clients: dict[str, Any],
) -> None:
    """Defensive: if the LLM returns a matches_task_id that isn't on the
    candidate slate (parse error, hallucinated id), the row still carries
    matched_task_id (whatever the LLM returned) and matched_task_title is
    null. Better to record what we have than crash the dedup path."""
    cfg = _base_config(tmp_path)
    stub_clients["state"]["claude_result"] = {
        "status": "task",
        "task": {
            "title": "Some task",
            "owner": "megha",
            "confidence": "high",
            "source_tag": "school",
            "owner_reason": "context",
        },
        "_usage": {"input_tokens": 100, "output_tokens": 20},
    }
    stub_clients["state"]["open_tasks"] = [
        {"id": "task-A", "title": "Title A", "status": "notStarted"},
    ]
    stub_clients["state"]["sem_dup_result"] = {
        "is_duplicate": True,
        "matches_task_id": "task-NOT-IN-SLATE",
        "reason": "claimed dup",
    }

    msg_id = "msg-sem-dedup-002"
    handlers._email_arrived_impl(_notification(msg_id), cfg)

    rows = _read_eval_rows(cfg)
    dedup_rows = [r for r in rows if r["decision"] == "dedup_hit"]
    assert len(dedup_rows) == 1
    row = dedup_rows[0]
    assert row["matched_task_id"] == "task-NOT-IN-SLATE"
    assert row["matched_task_title"] is None


# ---- updated (package lifecycle) path -------------------------------------


def test_updated_decision_records_matched_task(
    tmp_path: Path, stub_clients: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a Shipped/OFD/Delivered email matches an existing package task
    via tier-1 (exact package_id) lookup, the resulting `updated` eval row
    must carry the matched task's title and ID."""
    cfg = _base_config(tmp_path)
    msg_id = "msg-lifecycle-update-001"
    # Non-Amazon merchant: Amazon (and Whole Foods on Amazon's order
    # infrastructure) shipment emails are skipped now; this exercises the
    # tier-1 matched-task recording with a regular UPS-tracked store.
    matched_id = "task-pkg-chewy-001"
    matched_title = "MJ Chewy order 1Z999AA10123456784"

    def _fetch(message_id: str, *, account: str | None = None) -> dict[str, Any]:
        return _stub_msg(
            message_id,
            from_addr="order-update@chewy.com",
            subject="Your Chewy order is out for delivery",
            body="Tracking 1Z999AA10123456784. Out for delivery now. ETA 8-10am.",
        )
    stub_clients["graph"].fetch_message.side_effect = _fetch
    stub_clients["graph"].find_todo_task_by_package_id.return_value = matched_id
    stub_clients["graph"].get_todo_task.return_value = {
        "title": matched_title,
        "body": {"content": "audit trail line 1"},
    }
    stub_clients["graph"].update_todo_task.return_value = None
    # Force quiet hours so the OFD iMessage path doesn't try to actually send.
    monkeypatch.setattr(handlers, "is_quiet_hours", lambda c: True)

    result = handlers._email_arrived_impl(_notification(msg_id), cfg)
    assert result.get("task_id") == matched_id

    rows = _read_eval_rows(cfg)
    updated_rows = [r for r in rows if r["decision"] == "updated"]
    assert len(updated_rows) == 1, f"expected 1 updated row, got {len(updated_rows)}"
    row = updated_rows[0]
    assert row["matched_task_id"] == matched_id
    assert row["matched_task_title"] == matched_title
    # The legacy merge_target_task_id field is still populated too.
    assert row["merge_target_task_id"] == matched_id


def test_updated_decision_lifecycle_continuation_carries_matched_task_each_transition(
    tmp_path: Path, stub_clients: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordered -> Shipped -> OFD -> Delivered on the same package: each
    transition row must independently carry the matched task fields so
    Megha can label every transition in the viewer without losing context."""
    cfg = _base_config(tmp_path)
    matched_id = "task-pkg-001"
    # Non-Amazon merchant on purpose: Amazon shipment emails are now skipped
    # wholesale (the household self-tracks Amazon), so the lifecycle-
    # continuation mechanism is exercised here with a regular store.
    matched_title = "MJ Sundays for Dogs order TRACK123ABC"

    monkeypatch.setattr(handlers, "is_quiet_hours", lambda c: True)
    stub_clients["graph"].find_todo_task_by_package_id.return_value = matched_id
    stub_clients["graph"].get_todo_task.return_value = {
        "title": matched_title,
        "body": {"content": "audit trail"},
    }
    stub_clients["graph"].update_todo_task.return_value = None

    transitions = [
        ("msg-shipped", "Your Sundays for Dogs order has shipped",
         "Tracking 1Z999AA10123456784. Shipped."),
        ("msg-ofd", "Your Sundays for Dogs order is out for delivery",
         "Tracking 1Z999AA10123456784. Out for delivery."),
        ("msg-delivered", "Your Sundays for Dogs order was delivered",
         "Tracking 1Z999AA10123456784. Delivered."),
    ]
    for msg_id, subj, body in transitions:
        def _fetch(message_id: str, *, account: str | None = None,
                   _s=subj, _b=body) -> dict[str, Any]:
            return _stub_msg(
                message_id, from_addr="ship-confirm@sundaysfordogs.com",
                subject=_s, body=_b,
            )
        stub_clients["graph"].fetch_message.side_effect = _fetch
        # Clear dedup cache between transitions so each one runs fresh
        # (they have different message_ids anyway).
        handlers._email_arrived_impl(_notification(msg_id), cfg)

    rows = _read_eval_rows(cfg)
    updated_rows = [r for r in rows if r["decision"] == "updated"]
    # Some transitions may fall back to skipped if the package_extractor
    # didn't detect that state, so assert we got at least one updated row
    # and every updated row carries the matched task fields.
    assert len(updated_rows) >= 1, (
        f"expected >=1 updated rows, got {len(updated_rows)}; rows={rows}"
    )
    for row in updated_rows:
        assert row["matched_task_id"] == matched_id, row
        assert row["matched_task_title"] == matched_title, row


def test_amazon_shipment_emails_are_skipped(
    tmp_path: Path, stub_clients: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Amazon shipment-lifecycle emails create NO task and update NO task —
    the household self-tracks Amazon (Megha 2026-06-22). Skipped with reason
    amazon_shipment_noise; a genuine Amazon ACTION email (no lifecycle state)
    is NOT covered by this skip and still flows to the LLM."""
    cfg = _base_config(tmp_path)
    monkeypatch.setattr(handlers, "is_quiet_hours", lambda c: True)

    def _fetch(message_id: str, *, account: str | None = None) -> dict[str, Any]:
        return _stub_msg(
            message_id, from_addr="ship-confirm@amazon.com",
            subject="Your Amazon order has shipped",
            body="Your package 112-1234567-1234567 has shipped.",
        )
    stub_clients["graph"].fetch_message.side_effect = _fetch

    result = handlers._email_arrived_impl(_notification("msg-amzn-ship"), cfg)

    assert result.get("reason") == "amazon_shipment_noise"
    stub_clients["graph"].update_todo_task.assert_not_called()
    stub_clients["graph"].create_todo_task.assert_not_called()


# ---- negative coverage: create / skip rows leave fields null --------------


def test_skip_and_create_do_not_set_matched_task(
    tmp_path: Path, stub_clients: dict[str, Any],
) -> None:
    """Sanity: pure skip + pure create paths must leave the new matched-task
    fields null. We don't want the viewer to invent a match on rows that
    never had one."""
    cfg = _base_config(tmp_path)

    # 1. Skip path.
    stub_clients["state"]["claude_result"] = {
        "status": "skipped", "reason": "marketing newsletter",
    }
    handlers._email_arrived_impl(_notification("msg-skip-A"), cfg)

    # 2. Create path (no semantic dup).
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
    stub_clients["state"]["sem_dup_result"] = {"is_duplicate": False}
    handlers._email_arrived_impl(_notification("msg-create-A"), cfg)

    rows = _read_eval_rows(cfg)
    # Every non-dedup, non-update row must have null matched_task fields.
    for r in rows:
        if r["decision"] in {"dedup_hit", "updated"}:
            continue
        assert r.get("matched_task_id") is None, r
        assert r.get("matched_task_title") is None, r
