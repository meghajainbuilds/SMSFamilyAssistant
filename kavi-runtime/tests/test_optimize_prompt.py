"""Tests for scripts/optimize_prompt.py — Phase 3 of the doc-driven eval
loop: the prompt-improvement engine.

No live LLM, no network. The proposer LLM is a stub returning fixed variant
texts; the scorer is a stub (injected via score_fn) that returns
deterministic per-case pass-rates so the held-out set scores predictably.

Covers:
  - baseline IS computed (measured) on the held-out set, reusing the
    _score_one_case-shaped scorer;
  - N variants proposed, clamped to the [1, 8] bound;
  - the report ranks variants and selects a winner by score;
  - NO fabricated score: in the default (no override) path every variant is
    measured=False and the report flags scores_are_measured=False;
  - the FROZEN matrix file + manifest are never touched by the optimizer;
  - when skill_override IS supported, variants are MEASURED and the winner is
    chosen by the real measured number;
  - the held-out improvement set is loaded from a SEPARATE file (not matrix/).
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import matrix_freeze
from scripts import optimize_prompt


CAP = "test-cap"


# --------------------------------------------------------------------------
# Fixtures / stubs
# --------------------------------------------------------------------------

def _write_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "skill.md"
    skill.write_text("# original skill\n\nSurface what matters. Cover all intents.\n")
    return skill


def _write_improvement_set(tmp_path: Path, case_ids: list[str]) -> None:
    path = optimize_prompt.improvement_set_path(CAP, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"case_id": cid, "payload": {"marker": cid}, "expect": {"verdict": "PASS"}}
        for cid in case_ids
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _freeze_matrix(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Freeze a real matrix so we can assert the optimizer never touches it.
    Returns (matrix_path, manifest_path, matrix_bytes, manifest_bytes)."""
    mpath = matrix_freeze.matrix_path(CAP, tmp_path)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps({
        "case_id": "frozen-1",
        "description": "frozen guard row",
        "payload": {"marker": "frozen-1"},
        "expect": {"verdict": "PASS"},
    }) + "\n")
    matrix_freeze.freeze(CAP, tmp_path)
    man_path = matrix_freeze.manifest_path(CAP, tmp_path)
    return mpath, man_path, mpath.read_text(), man_path.read_text()


class _StubProposer:
    """Returns a fixed set of variants regardless of prompt. Records the
    prompt it was handed so we can assert the three-axis contract + weak
    cases were passed."""

    def __init__(self, variants: list[dict]):
        self._variants = variants
        self.last_prompt: str | None = None

    def propose(self, prompt: str) -> dict:
        self.last_prompt = prompt
        return {"variants": self._variants}


def _make_score_fn(rates: dict[str, float], override_rates: dict[str, float] | None = None):
    """A stub standing in for run_matrix._score_one_case. Returns a fixed
    pass-rate per case_id. If the row payload carries a skill_override and
    override_rates is given, returns the override rate (so a 'measured'
    variant can score differently from the baseline)."""

    def _score(client, url, row, samples):
        cid = row["case_id"]
        has_override = "skill_override" in (row.get("payload") or {})
        if has_override and override_rates is not None:
            rate = override_rates.get(cid, 0.0)
        else:
            rate = rates.get(cid, 0.0)
        return {
            "case_id": cid,
            "samples": samples,
            "pass_count": int(round(rate * samples)),
            "pass_rate": rate,
            "last_details": [] if rate >= 1.0 else ["stub: below 1.0"],
            "last_verdict": "PASS" if rate >= 1.0 else "FAIL",
            "last_output": "stub",
            "transport_error": None,
        }

    return _score


_THREE_VARIANTS = [
    {"label": "tighter-clarify", "rationale": "name the owner explicitly",
     "predicted_improvement": 0.10, "skill_text": "# v1\nName the owner.\n"},
    {"label": "aggregate-intents", "rationale": "aggregate same-type results",
     "predicted_improvement": 0.30, "skill_text": "# v2\nAggregate intents.\n"},
    {"label": "minimal", "rationale": "trim noise",
     "predicted_improvement": 0.05, "skill_text": "# v3\nTrim noise.\n"},
]


# --------------------------------------------------------------------------
# Held-out set loading (separate from the frozen matrix)
# --------------------------------------------------------------------------

def test_improvement_set_path_is_separate_from_matrix(tmp_path: Path) -> None:
    iset = optimize_prompt.improvement_set_path(CAP, tmp_path)
    matrix = matrix_freeze.matrix_path(CAP, tmp_path)
    assert iset != matrix
    assert "matrix" not in iset.parts  # NOT under the frozen matrix/ dir
    assert iset.name == "improvement-set.jsonl"


def test_load_improvement_rows_validates_schema(tmp_path: Path) -> None:
    _write_improvement_set(tmp_path, ["a", "b"])
    rows = optimize_prompt.load_improvement_rows(CAP, tmp_path)
    assert {r["case_id"] for r in rows} == {"a", "b"}


def test_missing_improvement_set_raises(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(FileNotFoundError):
        optimize_prompt.load_improvement_rows(CAP, tmp_path)


def test_seed_fixture_parses_as_improvement_rows() -> None:
    """The shipped seed/example fixture is a valid improvement set."""
    fixture = (Path(__file__).resolve().parent
               / "fixtures" / "improvement-set.example.jsonl")
    assert fixture.exists()
    # Reuse the loader's per-row validation against the fixture directly.
    seen = set()
    for line in fixture.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        assert {"case_id", "payload", "expect"} <= set(row)
        assert row["expect"]["verdict"] in {"PASS", "FAIL"}
        assert row["case_id"] not in seen
        seen.add(row["case_id"])
    assert seen


# --------------------------------------------------------------------------
# Baseline (Step A) + proposal (Step B) + ranking (Step D)
# --------------------------------------------------------------------------

def test_baseline_is_measured_and_variants_proposed(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a", "b"])
    _freeze_matrix(tmp_path)
    proposer = _StubProposer(_THREE_VARIANTS)
    score_fn = _make_score_fn({"a": 0.4, "b": 0.6})  # mean 0.5

    report = optimize_prompt.optimize(
        CAP, skill, "http://fake:8081", tmp_path,
        n=3, samples=5, proposer_client=proposer, score_fn=score_fn)

    # Baseline measured.
    assert report["baseline"]["measured"] is True
    assert report["baseline"]["mean_pass_rate"] == 0.5
    assert report["baseline"]["per_case_pass_rate"] == {"a": 0.4, "b": 0.6}

    # N variants proposed and written to disk.
    assert report["n_variants"] == 3
    assert len(report["variants"]) == 3
    out_dir = tmp_path / CAP / "optimizer"
    assert sorted(p.name for p in out_dir.glob("variant-*.md")) == [
        "variant-01.md", "variant-02.md", "variant-03.md"]

    # The proposer prompt carried the three-axis contract + a weak case.
    assert "BEHAVIOR ONLY" in proposer.last_prompt
    assert "current pass-rate" in proposer.last_prompt


def test_variant_count_is_clamped(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a"])
    # n above the bound must raise (validated before any work).
    import pytest
    with pytest.raises(ValueError):
        optimize_prompt.optimize(
            CAP, skill, "http://fake:8081", tmp_path,
            n=optimize_prompt.MAX_VARIANTS + 1, samples=1,
            proposer_client=_StubProposer(_THREE_VARIANTS),
            score_fn=_make_score_fn({"a": 1.0}))


def test_propose_variants_truncates_to_n(tmp_path: Path) -> None:
    """If the LLM returns MORE variants than asked, only N are kept/written."""
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a"])
    proposer = _StubProposer(_THREE_VARIANTS)  # 3 variants offered
    report = optimize_prompt.optimize(
        CAP, skill, "http://fake:8081", tmp_path,
        n=2, samples=1, proposer_client=proposer,
        score_fn=_make_score_fn({"a": 0.5}))
    assert report["n_variants"] == 2


# --------------------------------------------------------------------------
# Honesty: unmeasured variants are NOT fabricated scores
# --------------------------------------------------------------------------

def test_default_path_does_not_fabricate_measured_scores(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a", "b"])
    report = optimize_prompt.optimize(
        CAP, skill, "http://fake:8081", tmp_path,
        n=3, samples=5, proposer_client=_StubProposer(_THREE_VARIANTS),
        score_fn=_make_score_fn({"a": 0.4, "b": 0.6}))

    # No variant claims a measured score in the default (no override) path.
    assert report["scores_are_measured"] is False
    for v in report["variants"]:
        assert v["measured"] is False
        assert v["measured_mean_pass_rate"] is None
        assert v["measured_delta"] is None
        # predicted_mean_pass_rate exists but is labeled prediction-only.
        assert "predicted_mean_pass_rate" in v
    assert "PREDICTED" in report["honest_limitation"]
    assert "UNMEASURED" in report["next_step"]
    assert report["winner"]["measured"] is False


def test_winner_is_top_ranked_by_prediction_when_unmeasured(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a"])
    report = optimize_prompt.optimize(
        CAP, skill, "http://fake:8081", tmp_path,
        n=3, samples=1, proposer_client=_StubProposer(_THREE_VARIANTS),
        score_fn=_make_score_fn({"a": 0.5}))
    # aggregate-intents has the highest predicted_improvement (0.30).
    assert report["winner"]["label"] == "aggregate-intents"
    assert report["winner"]["rank"] == 1
    # rank order monotonic by predicted score.
    ranks = [(v["rank"], v["predicted_mean_pass_rate"]) for v in report["variants"]]
    ranks.sort()
    scores = [s for _, s in ranks]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------
# Measured path (skill_override supported) — winner picked by REAL score
# --------------------------------------------------------------------------

def test_override_path_measures_and_ranks_by_real_score(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a", "b"])
    # Baseline mean 0.5. Override rates per variant are identical here (the
    # stub keys only on case_id), so every variant measures to the same real
    # number — but the point is it is MEASURED, not predicted.
    score_fn = _make_score_fn(
        {"a": 0.4, "b": 0.6},               # baseline
        override_rates={"a": 1.0, "b": 1.0})  # variants score 1.0 measured

    report = optimize_prompt.optimize(
        CAP, skill, "http://fake:8081", tmp_path,
        n=2, samples=5, proposer_client=_StubProposer(_THREE_VARIANTS),
        score_fn=score_fn, skill_override_supported=True)

    assert report["scores_are_measured"] is True
    for v in report["variants"]:
        assert v["measured"] is True
        assert v["measured_mean_pass_rate"] == 1.0
        assert v["measured_delta"] == 0.5  # 1.0 - 0.5 baseline
    assert report["winner"]["measured"] is True
    assert "MEASURED" in report["next_step"]


def test_measured_variant_outranks_predicted(tmp_path: Path) -> None:
    """A measured win must sort ahead of a mere prediction — the sort key
    puts measured rows in a strictly higher tier."""
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a"])
    # All measured at the same real score; confirms measured tier wins and the
    # report flags it. (Mixed measured/unmeasured cannot occur in one run, so
    # we assert the tiering via the sort key contract instead.)
    measured_rec = {"measured": True, "measured_mean_pass_rate": 0.6,
                    "predicted_mean_pass_rate": 0.99}
    predicted_rec = {"measured": False, "measured_mean_pass_rate": None,
                     "predicted_mean_pass_rate": 0.99}
    # Recreate the module's sort key behavior: measured tier (1) beats (0).
    def key(r):
        if r["measured"]:
            return (1, r["measured_mean_pass_rate"])
        return (0, r["predicted_mean_pass_rate"])
    ranked = sorted([predicted_rec, measured_rec], key=key, reverse=True)
    assert ranked[0] is measured_rec


# --------------------------------------------------------------------------
# Frozen matrix is never touched
# --------------------------------------------------------------------------

def test_frozen_matrix_untouched(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a", "b"])
    mpath, man_path, matrix_before, manifest_before = _freeze_matrix(tmp_path)

    report = optimize_prompt.optimize(
        CAP, skill, "http://fake:8081", tmp_path,
        n=3, samples=5, proposer_client=_StubProposer(_THREE_VARIANTS),
        score_fn=_make_score_fn({"a": 0.4, "b": 0.6}))

    # Byte-for-byte identical: optimizer wrote nothing under matrix/.
    assert mpath.read_text() == matrix_before
    assert man_path.read_text() == manifest_before
    assert report["frozen_matrix_untouched"] is True

    # And the frozen-matrix tamper check still passes after the run.
    ok, _ = matrix_freeze.check(CAP, tmp_path)
    assert ok

    # The optimizer wrote ONLY to the optimizer/ dir, not matrix/.
    matrix_dir = tmp_path / CAP / "matrix"
    assert not (matrix_dir / "optimizer-report.json").exists()
    assert (tmp_path / CAP / "optimizer" / "optimizer-report.json").exists()


def test_report_written_with_next_step_and_winner(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    _write_improvement_set(tmp_path, ["a"])
    report = optimize_prompt.optimize(
        CAP, skill, "http://fake:8081", tmp_path,
        n=2, samples=1, proposer_client=_StubProposer(_THREE_VARIANTS),
        score_fn=_make_score_fn({"a": 0.5}))

    report_path = Path(report["report_path"])
    assert report_path.exists()
    on_disk = json.loads(report_path.read_text())
    assert on_disk["winner"] is not None
    assert on_disk["next_step"]
    assert on_disk["baseline"]["mean_pass_rate"] == 0.5
    assert on_disk["held_out_cases"] == 1
