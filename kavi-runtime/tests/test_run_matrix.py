"""Tests for scripts/run_matrix.py — the bounded-iteration matrix runner
of the capability-build pipeline (capabilities/BUILD_PIPELINE.md).

Covers: row pass/fail logic (verdict + must_contain_any +
must_not_contain, including dict outputs for inbox-to-task), refusal to
run when the frozen-matrix check fails, the per-invocation iteration
counter and the >10-runs bound (runs but exits nonzero), --max-failures
early stop, run-log JSONL output, and host selection (--prod banner
host). HTTP is faked via an injected client — no network, no live hosts.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import matrix_freeze
from scripts import run_matrix


CAP = "test-cap"


def _row(case_id: str, expect: dict | None = None) -> dict:
    return {
        "case_id": case_id,
        "description": "test case",
        "payload": {"marker": case_id},
        "expect": expect or {"verdict": "PASS"},
    }


def _freeze(tmp_path: Path, rows: list[dict]) -> None:
    path = matrix_freeze.matrix_path(CAP, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    matrix_freeze.freeze(CAP, tmp_path)


class _FakeResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._body


class _FakeClient:
    """Stands in for httpx.Client. Responds per-case via a lookup on the
    posted payload's 'marker' field; records every call."""

    def __init__(self, responses: dict[str, dict], default: dict | None = None):
        self.responses = responses
        self.default = default or {"verdict": "PASS", "output": "ok", "failures": []}
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict) -> _FakeResponse:  # noqa: A002
        self.calls.append((url, json))
        marker = json.get("marker", "")
        return _FakeResponse(self.responses.get(marker, self.default))

    def close(self) -> None:
        pass


# ---- evaluate_row unit coverage ---------------------------------------------

def test_evaluate_row_passes_on_matching_verdict() -> None:
    row = _row("a", {"verdict": "PASS"})
    passed, details = run_matrix.evaluate_row(row, {"verdict": "PASS", "output": "x"})
    assert passed and details == []


def test_evaluate_row_fails_on_verdict_mismatch_with_gate_detail() -> None:
    row = _row("a", {"verdict": "PASS"})
    passed, details = run_matrix.evaluate_row(
        row,
        {"verdict": "FAIL", "output": "x",
         "failures": [{"gate": "owner_leak", "detail": "leaked"}]},
    )
    assert not passed
    assert any("verdict mismatch" in d for d in details)
    assert any("owner_leak" in d for d in details)


def test_evaluate_row_must_contain_any() -> None:
    row = _row("a", {"verdict": "PASS", "must_contain_any": ["Boonli", "BCBA"]})
    passed, _ = run_matrix.evaluate_row(
        row, {"verdict": "PASS", "output": "Boonli invoice due tomorrow."})
    assert passed
    passed, details = run_matrix.evaluate_row(
        row, {"verdict": "PASS", "output": "Quiet morning."})
    assert not passed
    assert any("must_contain_any" in d for d in details)


def test_evaluate_row_must_not_contain() -> None:
    row = _row("a", {"verdict": "PASS", "must_not_contain": ["closed it"]})
    passed, details = run_matrix.evaluate_row(
        row, {"verdict": "PASS", "output": "I closed it for you."})
    assert not passed
    assert any("must_not_contain" in d for d in details)


def test_evaluate_row_normalizes_dict_output() -> None:
    """inbox-to-task verify returns a dict output; substring checks apply
    to its JSON serialization."""
    row = _row("a", {"verdict": "PASS", "must_contain_any": ["Family reader"]})
    passed, _ = run_matrix.evaluate_row(
        row,
        {"verdict": "PASS",
         "output": {"status": "task", "task": {"title": "Cover Family reader slot"}}},
    )
    assert passed


def test_evaluate_row_none_output_fails_contain_check() -> None:
    row = _row("a", {"verdict": "PASS", "must_contain_any": ["cash"]})
    passed, _ = run_matrix.evaluate_row(row, {"verdict": "PASS", "output": None})
    assert not passed


# ---- run_matrix end-to-end (fake HTTP) --------------------------------------

def test_all_rows_pass_exits_zero_and_writes_run_log(tmp_path: Path) -> None:
    _freeze(tmp_path, [_row("a"), _row("b")])
    client = _FakeClient({})
    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, http_client=client)
    assert code == 0
    assert len(client.calls) == 2
    assert client.calls[0][0] == f"http://fake:8081/synthetic/verify/{CAP}"

    runs_dir = tmp_path / CAP / "matrix" / "runs"
    logs = sorted(runs_dir.glob("run-*.jsonl"))
    assert len(logs) == 1
    lines = [json.loads(l) for l in logs[0].read_text().splitlines()]
    header = lines[0]
    assert header["cases_passed"] == 2 and header["cases_failed"] == 0
    assert header["capability"] == CAP
    assert {r["case_id"] for r in lines[1:]} == {"a", "b"}


def test_row_failure_exits_one(tmp_path: Path) -> None:
    _freeze(tmp_path, [_row("a"), _row("b")])
    client = _FakeClient({"b": {"verdict": "FAIL", "output": "bad",
                               "failures": [{"gate": "g", "detail": "d"}]}})
    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, http_client=client)
    assert code == 1


def test_refuses_to_run_when_matrix_tampered(tmp_path: Path) -> None:
    """Anti-Goodhart rule #1: the runner makes ZERO HTTP calls when the
    frozen-matrix check fails."""
    _freeze(tmp_path, [_row("a")])
    matrix_path = matrix_freeze.matrix_path(CAP, tmp_path)
    matrix_path.write_text(
        matrix_path.read_text()
        + json.dumps(_row("sneaky-new-case")) + "\n"
    )
    client = _FakeClient({})
    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, http_client=client)
    assert code == 1
    assert client.calls == []


def test_refuses_to_run_when_never_frozen(tmp_path: Path) -> None:
    path = matrix_freeze.matrix_path(CAP, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_row("a")) + "\n")
    client = _FakeClient({})
    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, http_client=client)
    assert code == 1
    assert client.calls == []


def test_iteration_counter_increments_per_invocation(tmp_path: Path) -> None:
    _freeze(tmp_path, [_row("a")])
    for expected in (1, 2, 3):
        run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path,
                              http_client=_FakeClient({}))
        counter = json.loads(
            matrix_freeze.iteration_count_path(CAP, tmp_path).read_text())
        assert counter["runs_since_freeze"] == expected


def test_iteration_bound_exceeded_runs_but_exits_nonzero(tmp_path: Path) -> None:
    """Anti-Goodhart rule #2: past ITERATION_BOUND runs since freeze the
    matrix still executes (process, not silence) but exit goes nonzero
    even when every row passes."""
    _freeze(tmp_path, [_row("a")])
    counter_path = matrix_freeze.iteration_count_path(CAP, tmp_path)
    counter = json.loads(counter_path.read_text())
    counter["runs_since_freeze"] = run_matrix.ITERATION_BOUND
    counter_path.write_text(json.dumps(counter))

    client = _FakeClient({})
    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, http_client=client)
    assert code == 3  # green rows, exceeded bound
    assert len(client.calls) == 1  # the run still executed

    header = json.loads(
        sorted((tmp_path / CAP / "matrix" / "runs").glob("run-*.jsonl"))[-1]
        .read_text().splitlines()[0]
    )
    assert header["bound_exceeded"] is True
    assert header["cases_failed"] == 0


def test_freeze_resets_bound(tmp_path: Path) -> None:
    _freeze(tmp_path, [_row("a")])
    counter_path = matrix_freeze.iteration_count_path(CAP, tmp_path)
    counter = json.loads(counter_path.read_text())
    counter["runs_since_freeze"] = run_matrix.ITERATION_BOUND + 5
    counter_path.write_text(json.dumps(counter))

    matrix_freeze.freeze(CAP, tmp_path)  # re-freeze resets
    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path,
                                 http_client=_FakeClient({}))
    assert code == 0


def test_max_failures_early_stop(tmp_path: Path) -> None:
    _freeze(tmp_path, [_row("a"), _row("b"), _row("c")])
    failing = {"verdict": "FAIL", "output": "bad", "failures": []}
    client = _FakeClient({"a": failing, "b": failing, "c": failing})
    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path,
                                 max_failures=1, http_client=client)
    assert code == 1
    assert len(client.calls) == 1


def test_http_error_counts_as_failure(tmp_path: Path) -> None:
    _freeze(tmp_path, [_row("a")])

    class _ErrClient(_FakeClient):
        def post(self, url, json):  # noqa: A002
            self.calls.append((url, json))
            return _FakeResponse({}, status_code=500)

    code = run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path,
                                 http_client=_ErrClient({}))
    assert code == 1


def test_default_hosts() -> None:
    """Staging port 8081 is the default target; --prod swaps to 8080."""
    assert run_matrix.DEFAULT_STAGING_HOST.endswith(":8081")
    assert run_matrix.PROD_HOST.endswith(":8080")


# ---- multi-sample graded scoring (Phase 1: a number that climbs) ------------


class _FlakyClient(_FakeClient):
    """Returns a CYCLING sequence of responses per marker, so the same case
    can pass on some samples and fail on others — the bare-yea flakiness the
    multi-sample scorecard is designed to expose."""

    def __init__(self, sequences: dict[str, list[dict]],
                 default: dict | None = None):
        super().__init__({}, default)
        self.sequences = sequences
        self._idx: dict[str, int] = {}

    def post(self, url: str, json: dict):  # noqa: A002
        self.calls.append((url, json))
        marker = json.get("marker", "")
        seq = self.sequences.get(marker)
        if not seq:
            return _FakeResponse(self.default)
        i = self._idx.get(marker, 0)
        self._idx[marker] = i + 1
        return _FakeResponse(seq[i % len(seq)])


_PASS = {"verdict": "PASS", "output": "ok", "failures": []}
_FAIL = {"verdict": "FAIL", "output": "bad", "failures": []}


def test_multisample_pass_rate_on_flaky_case(tmp_path: Path) -> None:
    """A case that passes on half its samples scores 0.5 — a stable number,
    not a coin flip. Each case is hit `samples` times."""
    _freeze(tmp_path, [_row("flaky"), _row("solid")])
    client = _FlakyClient({"flaky": [_PASS, _FAIL]})  # alternates -> 0.5
    run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path,
                          samples=4, pass_threshold=0.5, http_client=client)
    assert len(client.calls) == 8  # 2 cases x 4 samples

    sc_path = tmp_path / CAP / "matrix" / "scorecard.jsonl"
    entry = json.loads(sc_path.read_text().splitlines()[-1])
    assert entry["samples"] == 4
    assert entry["per_case_pass_rate"]["flaky"] == 0.5
    assert entry["per_case_pass_rate"]["solid"] == 1.0
    assert entry["mean_pass_rate"] == 0.75


def test_pass_threshold_gates_exit_code(tmp_path: Path) -> None:
    """A 0.5 flaky case is green at threshold 0.5, red at threshold 1.0."""
    _freeze(tmp_path, [_row("flaky")])
    code_lenient = run_matrix.run_matrix(
        CAP, "http://fake:8081", tmp_path, samples=4, pass_threshold=0.5,
        http_client=_FlakyClient({"flaky": [_PASS, _FAIL]}))
    assert code_lenient == 0

    matrix_freeze.freeze(CAP, tmp_path)  # reset bound
    code_strict = run_matrix.run_matrix(
        CAP, "http://fake:8081", tmp_path, samples=4, pass_threshold=1.0,
        http_client=_FlakyClient({"flaky": [_PASS, _FAIL]}))
    assert code_strict == 1


def test_scorecard_records_before_after_delta(tmp_path: Path) -> None:
    """Two graded runs leave two scorecard rows; the second is measured
    against the first (the recorded improvement signal)."""
    _freeze(tmp_path, [_row("c")])
    run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, samples=2,
                          pass_threshold=0.5,
                          http_client=_FlakyClient({"c": [_FAIL]}))
    matrix_freeze.freeze(CAP, tmp_path)
    run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, samples=2,
                          pass_threshold=0.5,
                          http_client=_FlakyClient({"c": [_PASS]}))

    rows = [json.loads(l) for l in
            (tmp_path / CAP / "matrix" / "scorecard.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["per_case_pass_rate"]["c"] == 0.0
    assert rows[1]["per_case_pass_rate"]["c"] == 1.0


def test_samples_one_writes_no_scorecard(tmp_path: Path) -> None:
    """Backward compat: single-sample runs keep pass/fail-only behavior and
    write no scorecard."""
    _freeze(tmp_path, [_row("a")])
    run_matrix.run_matrix(CAP, "http://fake:8081", tmp_path, samples=1,
                          http_client=_FakeClient({}))
    assert not (tmp_path / CAP / "matrix" / "scorecard.jsonl").exists()
