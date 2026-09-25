"""Tests for kavi_runtime.structured_log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kavi_runtime.structured_log import (
    VALID_CATEGORIES,
    iter_events,
    log_event,
    reset_handlers_for_test,
)


@pytest.fixture(autouse=True)
def _reset_log_handlers():
    """Each test gets a fresh handler cache so file handles into prior
    tmp_paths don't leak."""
    reset_handlers_for_test()
    yield
    reset_handlers_for_test()


def test_log_event_writes_one_json_line(tmp_path: Path) -> None:
    log_path = tmp_path / "kavi.json.log"
    log_event(
        "email", "email_arrived_start", path=log_path,
        account="megha@example.com", subscription_id="abc123",
    )
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["category"] == "email"
    assert row["event"] == "email_arrived_start"
    assert row["account"] == "megha@example.com"
    assert row["subscription_id"] == "abc123"
    assert "ts" in row


def test_log_event_appends_multiple_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "kavi.json.log"
    log_event("imessage", "imessage_received_start", path=log_path)
    log_event("imessage", "imessage_received_done", path=log_path)
    log_event("anthropic", "compose_call_done", path=log_path,
              call="compose_qa_question", input_tokens=1200, output_tokens=80)
    lines = log_path.read_text().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line).get("ts") for line in lines)
    third = json.loads(lines[2])
    assert third["call"] == "compose_qa_question"
    assert third["input_tokens"] == 1200


def test_log_event_swallows_write_errors(tmp_path: Path) -> None:
    """Write to a path under a file (not a dir) — Path.parent.mkdir(exist_ok=True)
    on a non-dir path is a no-op-ish, but the open() will fail. log_event must
    not raise."""
    blocking_file = tmp_path / "blocker"
    blocking_file.write_text("not a dir")
    bad_path = blocking_file / "nested" / "kavi.json.log"
    # Must not raise even though the path is not writable.
    log_event("error", "synthetic_failure_test", path=bad_path)


def test_log_event_uses_timed_rotating_handler(tmp_path: Path) -> None:
    """Item #4 (2026-05-06): the structured-log writer should attach a
    TimedRotatingFileHandler so the log file rotates daily with bounded
    retention. Without this the JSON log grows unbounded."""
    import logging.handlers

    from kavi_runtime import structured_log as sl

    log_path = tmp_path / "kavi.json.log"
    log_event("email", "rotation_smoke", path=log_path)

    # The handler cache should now contain a TimedRotatingFileHandler keyed
    # on the resolved path.
    handlers = list(sl._HANDLER_CACHE.values())
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    # Daily rotation, 30-day retention, midnight boundary.
    assert handler.when == "MIDNIGHT"
    assert handler.backupCount == 30


def test_rotation_handler_keeps_appending_to_same_file(tmp_path: Path) -> None:
    """Successive log_event calls should append to the same file (until
    midnight rolls the file). Regression check that the cached handler
    isn't being torn down per call (which would re-open the file each
    time and silently leak handles)."""
    log_path = tmp_path / "kavi.json.log"
    for i in range(5):
        log_event("email", "rotation_append_smoke", path=log_path, idx=i)
    lines = log_path.read_text().splitlines()
    assert len(lines) == 5
    parsed = [json.loads(line) for line in lines]
    assert [r["idx"] for r in parsed] == [0, 1, 2, 3, 4]


def test_iter_events_round_trips(tmp_path: Path) -> None:
    log_path = tmp_path / "kavi.json.log"
    log_event("webhook", "graph_subscription_validated", path=log_path)
    log_event("alert", "spend_cap_tripped", path=log_path,
              spend_usd=31.42, cap_usd=30)
    log_event("scheduler", "nightly_snapshot_done", path=log_path,
              copied=4, skipped=1)
    events = list(iter_events(log_path))
    assert len(events) == 3
    categories = [e["category"] for e in events]
    assert categories == ["webhook", "alert", "scheduler"]
    assert events[1]["spend_usd"] == 31.42
    assert all(c in VALID_CATEGORIES for c in categories)
