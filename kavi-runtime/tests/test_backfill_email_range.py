"""Tests for scripts.backfill_email_range.

Mocks the Graph client (date-range fetch + dedup + create) and the Claude
classifier so the suite never hits external services. Validates the five
properties the design contract guarantees:

  1. Each fetched email goes through the classifier exactly once.
  2. Tasks land in MS To Do for every `task`-shaped classifier result.
  3. One JSONL row is appended per processed email.
  4. The summary iMessage fires exactly ONCE and no per-task iMessage fires.
  5. The cost cap halts the run mid-batch when set artificially low.
  6. `--dry-run` does NOT call create_todo_task and does NOT write JSONL.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts import backfill_email_range as bf


# ---------- helpers ---------------------------------------------------------


def _make_msg(idx: int, subject: str = "test", sender: str = "alice@example.com") -> dict:
    return {
        "id": f"AQMkAD-msg-{idx:03d}",
        "subject": subject,
        "from": {"emailAddress": {"name": "Alice", "address": sender}},
        "toRecipients": [{"emailAddress": {"address": "megha@example.com"}}],
        "receivedDateTime": f"2026-05-{14 + idx % 12:02d}T10:00:00Z",
        "bodyPreview": f"body preview {idx}",
        "body": {"content": f"body content {idx}", "contentType": "text"},
        "conversationId": f"conv-{idx}",
        "parentFolderId": "inbox-folder",
        "internetMessageHeaders": [],
    }


def _classifier_result_task(idx: int, confidence: str = "high") -> dict:
    return {
        "status": "task",
        "task": {
            "title": f"Test task {idx}",
            "owner": "megha",
            "owner_reason": "test reason",
            "confidence": confidence,
            "source_tag": "test",
        },
        "_usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _classifier_result_skip(reason: str = "not_actionable") -> dict:
    return {
        "status": "skipped",
        "reason": reason,
        "_usage": {
            "input_tokens": 800,
            "output_tokens": 30,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


@pytest.fixture
def fake_config(tmp_path: Path) -> dict:
    """A minimal config that points all writeable paths inside tmp."""
    judgments_path = tmp_path / "eval-inbox-judgments.jsonl"
    state_path = tmp_path / "imessage-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "paths": {
            "eval_inbox_judgments_jsonl": str(judgments_path),
            "imessage_state": str(state_path),
            "runtime_events_jsonl": str(tmp_path / "runtime-events.jsonl"),
            "household_md": str(tmp_path / "household.md"),
        },
        "graph": {"mstodo_shared_list_id": "fake-list-id"},
        "imessage": {"megha_phone": "+15555550101", "own_email_addresses": ["megha@example.com"]},
    }


@pytest.fixture
def mock_graph() -> MagicMock:
    m = MagicMock()
    m.find_todo_task_by_source_email.return_value = None  # no exact-id dedup hit
    m.list_open_todo_tasks.return_value = []  # no open tasks -> skip semantic dedup
    m.create_todo_task.side_effect = lambda list_id, task, msg_id, subject, account=None: f"task-{msg_id[-3:]}"
    return m


@pytest.fixture
def mock_claude() -> MagicMock:
    m = MagicMock()
    m.check_semantic_duplicate.return_value = {"is_duplicate": False}
    return m


# ---------- the contract tests ---------------------------------------------


def test_processes_full_batch_creates_and_skips(
    fake_config: dict, mock_graph: MagicMock, mock_claude: MagicMock,
) -> None:
    """3 task + 2 skip results should yield 3 task creations and 5 JSONL rows."""
    messages = [_make_msg(i) for i in range(5)]
    classifier_outcomes = [
        _classifier_result_task(0),
        _classifier_result_skip(),
        _classifier_result_task(2),
        _classifier_result_skip(),
        _classifier_result_task(4),
    ]
    mock_claude.run_email_to_tasks.side_effect = classifier_outcomes

    with patch.object(bf, "list_messages_in_range", return_value=messages), \
         patch.object(bf, "_send_summary_imessage", return_value=True) as send_mock:
        result = bf.run_backfill(
            start="2026-05-14", end="2026-05-26",
            account="megha@example.com",
            dry_run=False, cost_cap_usd=10.0,
            config=fake_config, graph=mock_graph, claude=mock_claude,
        )

    assert result["processed"] == 5
    assert result["created"] == 3
    assert result["skipped"] == 2
    assert mock_graph.create_todo_task.call_count == 3
    assert mock_claude.run_email_to_tasks.call_count == 5

    # One JSONL row per processed email.
    judgments_path = Path(fake_config["paths"]["eval_inbox_judgments_jsonl"])
    rows = [json.loads(l) for l in judgments_path.read_text().splitlines() if l.strip()]
    assert len(rows) == 5
    assert all(r["backfill_source"] == bf.BACKFILL_TAG for r in rows)
    assert sum(1 for r in rows if r["decision"] == "created") == 3
    assert sum(1 for r in rows if r["decision"] == "skipped") == 2

    # Exactly one summary iMessage send.
    assert send_mock.call_count == 1
    summary_text = send_mock.call_args.args[1]
    assert "Backfill done" in summary_text
    assert "5 emails processed" in summary_text


def test_cost_cap_aborts_mid_run(
    fake_config: dict, mock_graph: MagicMock, mock_claude: MagicMock,
) -> None:
    """With an artificially tiny cap the run halts after the first email."""
    messages = [_make_msg(i) for i in range(5)]
    expensive = _classifier_result_task(0)
    # Inflate the usage so one call alone exceeds a cap of $0.01.
    expensive["_usage"] = {
        "input_tokens": 200_000, "output_tokens": 5_000,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    mock_claude.run_email_to_tasks.return_value = expensive

    with patch.object(bf, "list_messages_in_range", return_value=messages), \
         patch.object(bf, "_send_summary_imessage", return_value=True) as send_mock:
        result = bf.run_backfill(
            start="2026-05-14", end="2026-05-26",
            account="megha@example.com",
            dry_run=False, cost_cap_usd=0.01,
            config=fake_config, graph=mock_graph, claude=mock_claude,
        )

    assert result["aborted_cost_cap"] is True
    assert result["processed"] == 1
    assert mock_graph.create_todo_task.call_count == 1
    assert "stopped at cost cap" in result["summary_text"]
    # Summary still fires once even on cost-cap abort.
    assert send_mock.call_count == 1


def test_dry_run_does_not_create_or_log(
    fake_config: dict, mock_graph: MagicMock, mock_claude: MagicMock,
) -> None:
    """dry_run path must not touch MS To Do or eval-inbox-judgments.jsonl."""
    messages = [_make_msg(i) for i in range(3)]
    mock_claude.run_email_to_tasks.side_effect = [
        _classifier_result_task(0),
        _classifier_result_task(1),
        _classifier_result_skip(),
    ]

    with patch.object(bf, "list_messages_in_range", return_value=messages), \
         patch.object(bf, "_send_summary_imessage", return_value=True) as send_mock:
        result = bf.run_backfill(
            start="2026-05-14", end="2026-05-26",
            account="megha@example.com",
            dry_run=True, cost_cap_usd=10.0,
            config=fake_config, graph=mock_graph, claude=mock_claude,
        )

    assert result["processed"] == 3
    assert mock_graph.create_todo_task.call_count == 0
    assert mock_claude.check_semantic_duplicate.call_count == 0
    # No JSONL written.
    judgments_path = Path(fake_config["paths"]["eval_inbox_judgments_jsonl"])
    assert not judgments_path.exists()
    # No summary iMessage on dry-run either.
    assert send_mock.call_count == 0


def test_exact_id_dedup_does_not_recreate(
    fake_config: dict, mock_graph: MagicMock, mock_claude: MagicMock,
) -> None:
    """When find_todo_task_by_source_email returns an existing task id, the
    handler must NOT call create_todo_task. A `dedup_hit` row lands in the
    JSONL."""
    messages = [_make_msg(0)]
    mock_claude.run_email_to_tasks.return_value = _classifier_result_task(0)
    mock_graph.find_todo_task_by_source_email.return_value = "existing-task-id-001"

    with patch.object(bf, "list_messages_in_range", return_value=messages), \
         patch.object(bf, "_send_summary_imessage", return_value=True):
        result = bf.run_backfill(
            start="2026-05-14", end="2026-05-26",
            account="megha@example.com",
            dry_run=False, cost_cap_usd=10.0,
            config=fake_config, graph=mock_graph, claude=mock_claude,
        )

    assert result["dedup_hit"] == 1
    assert mock_graph.create_todo_task.call_count == 0
    rows = [json.loads(l) for l in Path(fake_config["paths"]["eval_inbox_judgments_jsonl"]).read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["decision"] == "dedup_hit"


def test_summary_text_shape() -> None:
    """The deterministic summary string must contain the four required
    counters and a plain-language CTA. No em dashes."""
    text = bf._summary_text(processed=42, created=9, skipped=33, runtime_sec=480.0)
    assert "Backfill done" in text
    assert "42 emails processed" in text
    assert "9 tasks created" in text
    assert "33 skipped" in text
    assert "Open MS To Do to review" in text
    assert "—" not in text  # No em dashes anywhere.


def test_checkpoint_resume_skips_processed(
    fake_config: dict, mock_graph: MagicMock, mock_claude: MagicMock, tmp_path: Path,
) -> None:
    """A pre-existing checkpoint file should cause the run to skip those
    message_ids entirely (no classifier call, no task create, no JSONL row)."""
    messages = [_make_msg(0), _make_msg(1), _make_msg(2)]
    mock_claude.run_email_to_tasks.return_value = _classifier_result_task(0)
    checkpoint_path = tmp_path / "ckpt.json"
    checkpoint_path.write_text(json.dumps({
        "backfill_tag": bf.BACKFILL_TAG,
        "updated_at": "2026-05-27T00:00:00Z",
        "processed_ids": ["AQMkAD-msg-000", "AQMkAD-msg-001"],
    }))

    with patch.object(bf, "list_messages_in_range", return_value=messages), \
         patch.object(bf, "_send_summary_imessage", return_value=True):
        result = bf.run_backfill(
            start="2026-05-14", end="2026-05-26",
            account="megha@example.com",
            dry_run=False, cost_cap_usd=10.0,
            config=fake_config, graph=mock_graph, claude=mock_claude,
            checkpoint_override=str(checkpoint_path),
        )

    # Only msg 2 should have been processed.
    assert result["processed"] == 1
    assert mock_claude.run_email_to_tasks.call_count == 1
    assert mock_graph.create_todo_task.call_count == 1
