"""Tests for scripts/matrix_freeze.py — the frozen-matrix half of the
capability-build pipeline (capabilities/BUILD_PIPELINE.md, anti-Goodhart
rule #1: the test matrix is hash-stamped before fix iteration begins and
the fix author cannot edit it without `check` failing loudly).

Covers: freeze/check round-trip, tamper detection, whitespace-insensitive
canonicalization, iteration-counter reset on freeze, row validation
(duplicate case_id, bad verdict, missing keys), and CLI exit codes.
All on temp dirs — no live evals/ mutation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import matrix_freeze


CAP = "test-cap"


def _row(case_id: str = "case-1", verdict: str = "PASS") -> dict:
    return {
        "case_id": case_id,
        "description": "a test case",
        "payload": {"time_of_day": "morning", "is_rollup": False},
        "expect": {"verdict": verdict, "must_contain_any": ["ok"]},
    }


def _write_matrix(evals_root: Path, rows: list[dict], cap: str = CAP) -> Path:
    path = matrix_freeze.matrix_path(cap, evals_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_freeze_then_check_round_trip(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("a"), _row("b")])
    manifest = matrix_freeze.freeze(CAP, tmp_path, timestamp="2026-06-10T00:00:00Z")
    assert manifest["capability"] == CAP
    assert manifest["case_count"] == 2
    assert manifest["frozen_at"] == "2026-06-10T00:00:00Z"
    assert len(manifest["sha256"]) == 64

    on_disk = json.loads(matrix_freeze.manifest_path(CAP, tmp_path).read_text())
    assert on_disk == manifest

    ok, message = matrix_freeze.check(CAP, tmp_path)
    assert ok, message
    assert "matrix OK" in message


def test_check_detects_tamper_after_freeze(tmp_path: Path) -> None:
    """Editing a row after freeze must flip check to a loud failure."""
    _write_matrix(tmp_path, [_row("a"), _row("b")])
    matrix_freeze.freeze(CAP, tmp_path)

    rows = [_row("a"), _row("b", verdict="FAIL")]  # fix author edits expectation
    _write_matrix(tmp_path, rows)

    ok, message = matrix_freeze.check(CAP, tmp_path)
    assert not ok
    assert "TAMPER" in message
    assert "re-freeze" in message or "freeze" in message


def test_check_detects_added_and_removed_cases(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("a"), _row("b")])
    matrix_freeze.freeze(CAP, tmp_path)

    _write_matrix(tmp_path, [_row("a")])  # case deleted
    ok, _ = matrix_freeze.check(CAP, tmp_path)
    assert not ok

    _write_matrix(tmp_path, [_row("a"), _row("b"), _row("c")])  # case added
    ok, _ = matrix_freeze.check(CAP, tmp_path)
    assert not ok


def test_canonicalization_is_whitespace_insensitive(tmp_path: Path) -> None:
    """Reformatting the JSONL (key order, spacing, blank lines) without
    changing content must NOT trip the tamper check."""
    _write_matrix(tmp_path, [_row("a")])
    matrix_freeze.freeze(CAP, tmp_path)

    row = _row("a")
    reordered = {k: row[k] for k in sorted(row, reverse=True)}
    matrix_freeze.matrix_path(CAP, tmp_path).write_text(
        "\n" + json.dumps(reordered, indent=2).replace("\n", " ") + "\n\n"
    )
    ok, message = matrix_freeze.check(CAP, tmp_path)
    assert ok, message


def test_freeze_resets_iteration_counter(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("a")])
    counter_path = matrix_freeze.iteration_count_path(CAP, tmp_path)
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(json.dumps({"runs_since_freeze": 7}))

    matrix_freeze.freeze(CAP, tmp_path)
    counter = json.loads(counter_path.read_text())
    assert counter["runs_since_freeze"] == 0
    assert counter["capability"] == CAP
    assert counter["frozen_sha256"] == json.loads(
        matrix_freeze.manifest_path(CAP, tmp_path).read_text()
    )["sha256"]


def test_check_fails_when_never_frozen(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("a")])
    ok, message = matrix_freeze.check(CAP, tmp_path)
    assert not ok
    assert "NO MANIFEST" in message


def test_check_fails_when_matrix_file_missing(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("a")])
    matrix_freeze.freeze(CAP, tmp_path)
    matrix_freeze.matrix_path(CAP, tmp_path).unlink()
    ok, message = matrix_freeze.check(CAP, tmp_path)
    assert not ok
    assert "unreadable" in message


def test_load_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("dup"), _row("dup")])
    with pytest.raises(ValueError, match="duplicate case_id"):
        matrix_freeze.load_matrix_rows(CAP, tmp_path)


def test_load_rejects_bad_verdict(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("a", verdict="MAYBE")])
    with pytest.raises(ValueError, match="expect.verdict"):
        matrix_freeze.load_matrix_rows(CAP, tmp_path)


def test_load_rejects_missing_required_keys(tmp_path: Path) -> None:
    row = _row("a")
    del row["payload"]
    _write_matrix(tmp_path, [row])
    with pytest.raises(ValueError, match="missing required keys"):
        matrix_freeze.load_matrix_rows(CAP, tmp_path)


def test_load_rejects_empty_matrix(tmp_path: Path) -> None:
    path = matrix_freeze.matrix_path(CAP, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n")
    with pytest.raises(ValueError, match="empty"):
        matrix_freeze.load_matrix_rows(CAP, tmp_path)


def test_cli_exit_codes(tmp_path: Path) -> None:
    _write_matrix(tmp_path, [_row("a")])
    root = str(tmp_path)
    assert matrix_freeze.main(["freeze", CAP, "--evals-root", root]) == 0
    assert matrix_freeze.main(["check", CAP, "--evals-root", root]) == 0

    # tamper -> nonzero
    _write_matrix(tmp_path, [_row("a"), _row("b")])
    assert matrix_freeze.main(["check", CAP, "--evals-root", root]) == 1

    # freeze of a missing matrix -> nonzero
    assert matrix_freeze.main(["freeze", "no-such-cap", "--evals-root", root]) == 1
