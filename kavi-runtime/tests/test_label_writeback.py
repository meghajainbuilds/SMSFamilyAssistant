"""Tests for scripts/label_writeback.py — the label -> doc + proposed-case
half of the Phase 2 doc-driven eval loop (the gradient).

Covers: a 'wrong' label appends a Bad example to the doc Behavior section
AND a proposed case to proposed-cases.jsonl; a 'right' label appends a Good
example; the learned subsection is created once and reused (idempotent
heading); the proposed case is matrix-schema-shaped; and the anti-Goodhart
property that the FROZEN matrix is never touched.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from scripts import label_writeback as lw


CAP = "demo-cap"
FROZEN_ROW = {
    "case_id": "frozen-keep-me",
    "description": "frozen row that must stay byte-for-byte unchanged",
    "payload": {"inbound_text": "hi", "sender": "megha", "open_tasks": []},
    "expect": {"verdict": "PASS"},
}
DOC = """\
# demo-cap

## TL;DR
A demo capability.

## Behavior

### Voice rules

#### Texture
- **Good:** Short and direct.
- **Bad:** Dense prose.

## Metrics
Threshold lives here; writeback must not land in this section.
"""


def _write_fixtures(tmp_path: Path) -> tuple[Path, Path]:
    evals_root = tmp_path / "evals"
    caps_root = tmp_path / "capabilities"
    mdir = evals_root / CAP / "matrix"
    mdir.mkdir(parents=True)
    caps_root.mkdir(parents=True)
    (caps_root / f"{CAP}.md").write_text(DOC)
    # A frozen matrix that must never be touched.
    (mdir / f"matrix-{CAP}.jsonl").write_text(json.dumps(FROZEN_ROW) + "\n")
    return evals_root, caps_root


def _behavior_section(caps_root: Path) -> str:
    text = (caps_root / f"{CAP}.md").read_text()
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("## Behavior"))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def test_wrong_label_appends_bad_example_and_proposed_case(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    result = lw.record_label(
        CAP,
        "regression-bare-yea-pending-fact-offer",
        "wrong",
        "should name the fact's topic",
        evals_root=evals_root,
        capabilities_root=caps_root,
        on=date(2026, 6, 24),
    )

    # (a) Bad example landed in the Behavior section, under the learned heading.
    section = _behavior_section(caps_root)
    assert lw.LEARNED_HEADING in section
    assert "**Bad:**" in section
    assert "should name the fact's topic" in section
    assert "regression-bare-yea-pending-fact-offer" in section
    # It did NOT leak into Metrics.
    full = (caps_root / f"{CAP}.md").read_text()
    metrics_part = full.split("## Metrics", 1)[1]
    assert "should name the fact's topic" not in metrics_part

    # (b) Proposed case appended, matrix-schema-shaped.
    proposed = Path(result["proposed_path"])
    assert proposed.exists()
    rows = [json.loads(l) for l in proposed.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    case = rows[0]
    assert set(case) >= {"case_id", "description", "payload", "expect"}
    assert case["expect"]["verdict"] in {"PASS", "FAIL"}
    assert isinstance(case["payload"], dict)
    assert "should name the fact's topic" in case["description"]


def test_right_label_appends_good_example(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    lw.record_label(
        CAP,
        "spec-seven-mark-done-one-message",
        "right",
        "delivered all seven cleanly",
        evals_root=evals_root,
        capabilities_root=caps_root,
        on=date(2026, 6, 24),
    )
    section = _behavior_section(caps_root)
    assert "**Good:**" in section
    assert "delivered all seven cleanly" in section
    assert lw.LEARNED_HEADING in section


def test_frozen_matrix_is_untouched(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    frozen = evals_root / CAP / "matrix" / f"matrix-{CAP}.jsonl"
    before = frozen.read_text()
    lw.record_label(
        CAP, "some-case", "wrong", "tighten the wording",
        evals_root=evals_root, capabilities_root=caps_root, on=date(2026, 6, 24),
    )
    assert frozen.read_text() == before
    assert json.loads(frozen.read_text().strip()) == FROZEN_ROW


def test_learned_heading_created_once_and_reused(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    for i in range(3):
        lw.record_label(
            CAP, f"case-{i}", "wrong", f"correction number {i}",
            evals_root=evals_root, capabilities_root=caps_root,
            on=date(2026, 6, 24),
        )
    section = _behavior_section(caps_root)
    # The learned heading appears exactly once.
    assert section.count(lw.LEARNED_HEADING) == 1
    # All three corrections are present as bullets.
    for i in range(3):
        assert f"correction number {i}" in section


def test_proposed_case_idempotent_on_same_label(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    on = date(2026, 6, 24)
    lw.record_label(
        CAP, "dup-case", "wrong", "first correction",
        evals_root=evals_root, capabilities_root=caps_root, on=on,
    )
    r2 = lw.record_label(
        CAP, "dup-case", "wrong", "second correction",
        evals_root=evals_root, capabilities_root=caps_root, on=on,
    )
    proposed = Path(r2["proposed_path"])
    rows = [json.loads(l) for l in proposed.read_text().splitlines() if l.strip()]
    # Same case_id (same ref + same day) -> replaced, not duplicated.
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))
    assert len(rows) == 1
    assert "second correction" in rows[0]["description"]


def test_bad_label_value_raises(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    try:
        lw.record_label(
            CAP, "c", "maybe", "x",
            evals_root=evals_root, capabilities_root=caps_root,
        )
    except ValueError as e:
        assert "label" in str(e).lower()
    else:
        raise AssertionError("expected ValueError on bad --label value")


def test_doc_edit_is_surgical_existing_content_preserved(tmp_path):
    """The original Texture Good/Bad bullets and Metrics section survive
    intact — writeback inserts, never rewrites."""
    evals_root, caps_root = _write_fixtures(tmp_path)
    lw.record_label(
        CAP, "c", "wrong", "new learned rule",
        evals_root=evals_root, capabilities_root=caps_root, on=date(2026, 6, 24),
    )
    full = (caps_root / f"{CAP}.md").read_text()
    assert "- **Good:** Short and direct." in full
    assert "- **Bad:** Dense prose." in full
    assert "## Metrics" in full
    assert "Threshold lives here" in full
