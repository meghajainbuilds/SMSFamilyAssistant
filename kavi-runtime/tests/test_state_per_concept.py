"""Tests for kavi_runtime.state_per_concept.

Covers:
- Round-trip save/load returns the exact data.
- Atomicity: concurrent saves never produce a partial read.
- Corruption recovery: malformed JSON is archived, load returns empty value.
- Idempotency: load on missing file returns empty value (no crash).
- Path mapping: each concept resolves to its expected per-concept filename.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from kavi_runtime import state_per_concept as spc


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Per-test state dir. The legacy path is passed to every helper as the
    way they locate the state directory."""
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def legacy_path(state_dir: Path) -> Path:
    return state_dir / "imessage-state.json"


# ---- path mapping ---------------------------------------------------------


def test_path_mapping_for_every_concept(legacy_path: Path) -> None:
    """Each concept's path resolves to a sibling of the legacy file."""
    legacy_dir = legacy_path.parent
    assert spc.questions_path(legacy_path) == legacy_dir / "questions.json"
    assert spc.summary_queue_path(legacy_path) == legacy_dir / "summary_queue.json"
    assert spc.pause_state_path(legacy_path) == legacy_dir / "pause_state.json"
    assert spc.pending_alerts_path(legacy_path) == legacy_dir / "pending_alerts.json"
    assert spc.action_clarifications_path(legacy_path) == legacy_dir / "action_clarifications.json"
    assert spc.alert_dedupe_path(legacy_path) == legacy_dir / "alert_dedupe.json"
    assert spc.self_check_path(legacy_path) == legacy_dir / "self_check.json"


# ---- round-trip per concept ----------------------------------------------


@pytest.mark.parametrize(
    "load_fn,save_fn,sample",
    [
        (
            spc.load_questions,
            spc.save_questions,
            {
                "questions": [{"id": "q-1", "task_id": "t-1"}],
                "last_send_at": "2026-06-02T12:00:00Z",
            },
        ),
        (
            spc.load_summary_queue,
            spc.save_summary_queue,
            {
                "summary_queue": [{"id": "s-1"}],
                "last_summary_send_at": "2026-06-02T04:00:00Z",
                "last_summary_anchors": {"+15555550101": {"anchor_task_id": "t-x"}},
            },
        ),
        (
            spc.load_pause_state,
            spc.save_pause_state,
            {
                "auto_runs_paused": True,
                "paused_since": "2026-06-02T01:00:00Z",
                "paused_reason": "spend_cap",
                "paused_email_queue": [{"id": "msg-1"}],
                "paused_spend_usd": 12.5,
                "spend_cap_bypass_month": "2026-06",
            },
        ),
        (
            spc.load_pending_alerts,
            spc.save_pending_alerts,
            {"pending_alerts": [{"key": "a-1", "queued_at": "2026-06-02T00:00:00Z"}]},
        ),
        (
            spc.load_action_clarifications,
            spc.save_action_clarifications,
            {
                "pending_action_clarifications": {
                    "+15555550101": {"action_type": "mark_done", "expires_at": "2026-06-02T01:00:00Z"}
                }
            },
        ),
        (
            spc.load_alert_dedupe,
            spc.save_alert_dedupe,
            {
                "alert_dedupe": {"sender|class": 123456.0},
                "last_failure_rate_alert_at": 123456.0,
            },
        ),
        (
            spc.load_self_check,
            spc.save_self_check,
            {"pending_self_check": {"sent_at": "2026-05-30T21:00:00Z"}},
        ),
    ],
)
def test_round_trip_returns_exact_data(legacy_path, load_fn, save_fn, sample) -> None:
    save_fn(legacy_path, sample)
    out = load_fn(legacy_path)
    # Every key in the input must be present and equal in the output.
    for k, v in sample.items():
        assert out[k] == v, f"key {k}: {out[k]!r} != {v!r}"


# ---- idempotent load on missing file -------------------------------------


def test_missing_file_returns_empty_defaults(legacy_path: Path) -> None:
    """Load on a missing file returns the default empty structure, not a crash."""
    out = spc.load_questions(legacy_path)
    assert out == {"questions": [], "last_send_at": None}

    out = spc.load_pending_alerts(legacy_path)
    assert out == {"pending_alerts": []}

    out = spc.load_action_clarifications(legacy_path)
    assert out == {"pending_action_clarifications": {}}


# ---- corruption recovery -------------------------------------------------


def test_corrupt_file_is_archived_and_defaults_returned(legacy_path: Path) -> None:
    """Malformed JSON in the canonical file is archived; load returns defaults."""
    questions_file = spc.questions_path(legacy_path)
    questions_file.write_text("{not valid json at all")

    out = spc.load_questions(legacy_path)
    # Defaults returned.
    assert out["questions"] == []
    assert out["last_send_at"] is None

    # Original file is gone; an archive sibling exists.
    assert not questions_file.exists()
    archived = list(questions_file.parent.glob("questions.json.corrupt-*"))
    assert len(archived) == 1, f"expected one archived corrupt file, got: {archived}"


def test_backfill_on_partial_file(legacy_path: Path) -> None:
    """A file written with a subset of the concept's keys is read with the
    missing keys backfilled to defaults."""
    pause_file = spc.pause_state_path(legacy_path)
    # Write only one field; reader should backfill the rest.
    pause_file.write_text(json.dumps({"auto_runs_paused": True}))

    out = spc.load_pause_state(legacy_path)
    assert out["auto_runs_paused"] is True
    assert out["paused_since"] is None
    assert out["paused_email_queue"] == []
    assert out["spend_cap_bypass_month"] is None


# ---- atomicity under concurrent writes -----------------------------------


def test_concurrent_writes_never_produce_partial_read(legacy_path: Path) -> None:
    """Spin up N threads each saving a slightly different payload to the
    same concept file. After all threads finish, every read returns a
    fully-valid JSON document (no torn writes)."""
    n_threads = 8
    n_iters = 25

    def writer(thread_id: int) -> None:
        for i in range(n_iters):
            spc.save_questions(
                legacy_path,
                {
                    "questions": [{"id": f"q-{thread_id}-{i}"}],
                    "last_send_at": f"2026-06-02T00:00:{i:02d}Z",
                },
            )

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()

    # Concurrently with the writers, repeatedly read. Every read must be
    # well-formed JSON with the expected shape (no torn writes).
    read_errors = []
    keep_reading = True

    def reader() -> None:
        while keep_reading:
            try:
                data = spc.load_questions(legacy_path)
                assert "questions" in data
                assert isinstance(data["questions"], list)
            except Exception as e:  # noqa: BLE001 - we capture for assertion
                read_errors.append(repr(e))

    reader_threads = [threading.Thread(target=reader) for _ in range(2)]
    for r in reader_threads:
        r.start()

    for t in threads:
        t.join()
    keep_reading = False
    for r in reader_threads:
        r.join()

    assert not read_errors, f"reads observed torn writes: {read_errors[:3]}"


def test_concurrent_writes_leave_no_tmp_files(legacy_path: Path) -> None:
    """After concurrent writes, no `.tmp*` siblings remain in the state dir
    (each writer's tmp was either renamed or deleted)."""
    def writer() -> None:
        for i in range(20):
            spc.save_summary_queue(
                legacy_path,
                {"summary_queue": [{"id": f"s-{i}"}], "last_summary_send_at": None, "last_summary_anchors": {}},
            )

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    leftover = list(legacy_path.parent.glob("summary_queue.json.*.tmp"))
    assert leftover == [], f"tmp files leaked: {leftover}"


# ---- needs_migration / run_boot_migration --------------------------------


def test_needs_migration_false_if_legacy_absent(legacy_path: Path) -> None:
    assert spc.needs_migration(legacy_path) is False


def test_needs_migration_true_when_legacy_present_but_per_concept_missing(
    legacy_path: Path,
) -> None:
    legacy_path.write_text(json.dumps({"questions": []}))
    assert spc.needs_migration(legacy_path) is True


def test_needs_migration_false_when_all_per_concept_present(legacy_path: Path) -> None:
    legacy_path.write_text(json.dumps({"questions": []}))
    # Create every per-concept file.
    for path_fn, empty in spc.CONCEPTS.values():
        path_fn(legacy_path).write_text(json.dumps(empty))
    assert spc.needs_migration(legacy_path) is False


def test_run_boot_migration_skipped_when_legacy_absent(legacy_path: Path) -> None:
    result = spc.run_boot_migration(legacy_path)
    assert result["event"] == "migration_phase3_skipped_already_migrated"


def test_run_boot_migration_completed_end_to_end(legacy_path: Path) -> None:
    legacy_path.write_text(
        json.dumps(
            {
                "questions": [{"id": "q-1"}],
                "summary_queue": [{"id": "s-1"}],
                "last_send_at": "2026-06-02T00:00:00Z",
                "last_summary_send_at": "2026-06-02T04:00:00Z",
                "last_summary_anchors": {},
                "auto_runs_paused": False,
                "paused_email_queue": [],
                "pending_alerts": [],
                "pending_action_clarifications": {},
                "alert_dedupe": {},
                "last_failure_rate_alert_at": None,
                "pending_self_check": {},
            }
        )
    )

    result = spc.run_boot_migration(legacy_path)

    assert result["event"] == "migration_phase3_completed"
    assert set(result["concepts_written"]) == set(spc.CONCEPTS.keys())
    assert result["concepts_skipped_already_present"] == []
    # Legacy file renamed to backup.
    assert not legacy_path.exists()
    assert (legacy_path.with_name(legacy_path.name + ".pre-phase3-backup")).exists()
    # Per-concept files all present.
    for path_fn, _ in spc.CONCEPTS.values():
        assert path_fn(legacy_path).exists()

    # Round-trip checks via load_*.
    assert spc.load_questions(legacy_path)["questions"] == [{"id": "q-1"}]
    assert spc.load_summary_queue(legacy_path)["summary_queue"] == [{"id": "s-1"}]
