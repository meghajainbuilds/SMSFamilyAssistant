"""Tests for scripts/gen_cases_from_spec.py — the doc -> proposed-cases
half of the Phase 2 doc-driven eval loop.

Covers: Behavior-section extraction, prose-example parsing, the LLM
converter is called WITH the endpoint-schema context (contract + real
matrix rows as few-shot), well-formed matrix-schema rows are written to
proposed-cases.jsonl (NOT the frozen matrix), stable/idempotent case_ids,
and the anti-Goodhart property that the frozen matrix is never touched.

The LLM is stubbed: a fake client records every messages.create call and
returns a canned matrix-schema row, so there is no network and no live
model call.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import gen_cases_from_spec as gcs


CAP = "demo-cap"


# --------------------------------------------------------------------------
# Tiny in-memory capability-doc fixture + frozen-matrix + contract.
# --------------------------------------------------------------------------
DOC = """\
# demo-cap

## TL;DR
A demo capability.

## Behavior

### Voice rules

#### Texture
- **Good:** Short and direct. Scannable in three seconds.
- **Bad:** Dense convoluted prose the reader re-reads.
- **Examples:**
  - Skip ack. Bad: over-worded multi-line reply. Good: "Sure, won't create one."

#### Few-shot 1 — Marketing skip.
Sender `nordstrom@eml.nordstrom.com`, subject "New Note." Output: skipped.

## Metrics
Not part of Behavior — must be excluded from extraction.
- **Good:** this Good line lives under Metrics and must NOT be picked up.
"""

REAL_MATRIX_ROW = {
    "case_id": "real-001",
    "description": "real frozen row used as payload-shape few-shot",
    "payload": {"inbound_text": "hi", "sender": "megha", "open_tasks": []},
    "expect": {"verdict": "PASS"},
}

CONTRACT = "# POST /synthetic/verify/demo-cap\nPayload: inbound_text, sender, open_tasks.\n"


def _write_fixtures(tmp_path: Path) -> tuple[Path, Path]:
    """Lay down evals_root + capabilities_root under tmp. Returns
    (evals_root, capabilities_root)."""
    evals_root = tmp_path / "evals"
    caps_root = tmp_path / "capabilities"
    mdir = evals_root / CAP / "matrix"
    mdir.mkdir(parents=True)
    caps_root.mkdir(parents=True)

    (caps_root / f"{CAP}.md").write_text(DOC)
    (mdir / f"matrix-{CAP}.jsonl").write_text(json.dumps(REAL_MATRIX_ROW) + "\n")
    (mdir / "ENDPOINT_CONTRACT.md").write_text(CONTRACT)
    return evals_root, caps_root


class _StubResponse:
    """Mimics the Anthropic SDK response: `.content[0].text`."""

    class _Block:
        def __init__(self, text: str):
            self.text = text

    def __init__(self, text: str):
        self.content = [self._Block(text)]


class _StubMessages:
    def __init__(self, parent: "_StubClient"):
        self._parent = parent

    def create(self, **kwargs):
        self._parent.calls.append(kwargs)
        # Echo a valid matrix row. case_id is intentionally junk — the script
        # must overwrite it with a stable slug.
        row = {
            "case_id": "LLM-SHOULD-OVERWRITE-THIS",
            "description": "converted by stub",
            "payload": {"inbound_text": "stub", "sender": "megha", "open_tasks": []},
            "expect": {"verdict": "PASS"},
        }
        return _StubResponse(json.dumps(row))


class _StubClient:
    """Injectable Anthropic-style client. No network."""

    def __init__(self):
        self.calls: list[dict] = []
        self.messages = _StubMessages(self)


# --------------------------------------------------------------------------
# Behavior extraction unit tests.
# --------------------------------------------------------------------------
def test_extract_behavior_section_stops_at_next_h2():
    section = gcs.extract_behavior_section(DOC)
    assert section.startswith("## Behavior")
    assert "Texture" in section
    # Metrics content must be excluded.
    assert "must NOT be picked up" not in section
    assert "## Metrics" not in section


def test_extract_examples_picks_good_bad_and_fewshot_only_in_behavior():
    section = gcs.extract_behavior_section(DOC)
    examples = gcs.extract_behavior_examples(section)
    blob = "\n".join(examples)
    # Good/Bad bullets and the few-shot block are captured.
    assert any("Short and direct" in e for e in examples)
    assert any("Few-shot 1" in e for e in examples)
    assert any("Skip ack" in e for e in examples)
    # The Good line under Metrics is NOT in the extracted set.
    assert "must NOT be picked up" not in blob


def test_stable_case_id_is_deterministic_and_prefixed():
    a = gcs.stable_case_id(CAP, "[Texture] Good: short and direct reply")
    b = gcs.stable_case_id(CAP, "[Texture] Good: short and direct reply")
    assert a == b
    assert a.startswith("gen-")
    # Different text -> different id.
    assert a != gcs.stable_case_id(CAP, "[Texture] Bad: dense prose")


# --------------------------------------------------------------------------
# End-to-end generate_cases: rows land in proposed-cases.jsonl, schema-valid,
# frozen matrix untouched, converter called with schema context.
# --------------------------------------------------------------------------
def test_generate_writes_proposed_not_frozen(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    client = _StubClient()

    cases = gcs.generate_cases(
        CAP, client, evals_root=evals_root, capabilities_root=caps_root
    )

    # Proposed file written.
    proposed = gcs.proposed_cases_path(CAP, evals_root)
    assert proposed.exists()
    lines = [l for l in proposed.read_text().splitlines() if l.strip()]
    assert len(lines) == len(cases) > 0

    # Frozen matrix is byte-for-byte untouched.
    frozen = evals_root / CAP / "matrix" / f"matrix-{CAP}.jsonl"
    assert json.loads(frozen.read_text().strip()) == REAL_MATRIX_ROW


def test_generated_rows_are_matrix_schema_valid(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    client = _StubClient()
    cases = gcs.generate_cases(
        CAP, client, evals_root=evals_root, capabilities_root=caps_root
    )
    for c in cases:
        assert set(c) >= {"case_id", "description", "payload", "expect"}
        assert isinstance(c["payload"], dict)
        assert c["expect"]["verdict"] in {"PASS", "FAIL"}
        # case_id is the stable slug, NOT the junk the stub returned.
        assert c["case_id"].startswith("gen-")
        assert c["case_id"] != "LLM-SHOULD-OVERWRITE-THIS"


def test_generated_proposed_file_loads_as_a_matrix(tmp_path):
    """The proposed file, copied to the frozen path, must satisfy
    matrix_freeze.load_matrix_rows — proof the rows are runnable as a matrix
    once the PM ratifies."""
    from scripts import matrix_freeze

    evals_root, caps_root = _write_fixtures(tmp_path)
    client = _StubClient()
    gcs.generate_cases(
        CAP, client, evals_root=evals_root, capabilities_root=caps_root
    )
    proposed = gcs.proposed_cases_path(CAP, evals_root)

    # Simulate the PM ratifying: copy proposed -> frozen path, then load.
    ratify_cap = "ratified-cap"
    rdir = evals_root / ratify_cap / "matrix"
    rdir.mkdir(parents=True)
    (rdir / f"matrix-{ratify_cap}.jsonl").write_text(proposed.read_text())
    rows = matrix_freeze.load_matrix_rows(ratify_cap, evals_root)
    assert len(rows) >= 1


def test_converter_called_with_endpoint_schema_context(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    client = _StubClient()
    gcs.generate_cases(
        CAP, client, evals_root=evals_root, capabilities_root=caps_root
    )
    assert client.calls, "converter was never called"
    for call in client.calls:
        prompt = call["messages"][0]["content"]
        # The ENDPOINT CONTRACT text is in the prompt.
        assert "POST /synthetic/verify/demo-cap" in prompt
        assert "ENDPOINT CONTRACT" in prompt
        # The real frozen row is shown as the payload-shape few-shot.
        assert "real-001" in prompt or "REAL EXAMPLE ROWS" in prompt
        # A model was passed.
        assert call.get("model")


def test_idempotent_rerun_updates_not_duplicates(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    client = _StubClient()
    first = gcs.generate_cases(
        CAP, client, evals_root=evals_root, capabilities_root=caps_root
    )
    second = gcs.generate_cases(
        CAP, client, evals_root=evals_root, capabilities_root=caps_root
    )
    # Same ids both runs, no growth.
    assert [c["case_id"] for c in first] == [c["case_id"] for c in second]
    proposed = gcs.proposed_cases_path(CAP, evals_root)
    lines = [l for l in proposed.read_text().splitlines() if l.strip()]
    assert len(lines) == len(second)
    # All ids unique within the file.
    ids = [json.loads(l)["case_id"] for l in lines]
    assert len(ids) == len(set(ids))


def test_missing_behavior_section_raises(tmp_path):
    evals_root, caps_root = _write_fixtures(tmp_path)
    (caps_root / f"{CAP}.md").write_text("# demo-cap\n\n## TL;DR\nno behavior here.\n")
    client = _StubClient()
    try:
        gcs.generate_cases(
            CAP, client, evals_root=evals_root, capabilities_root=caps_root
        )
    except ValueError as e:
        assert "Behavior" in str(e)
    else:
        raise AssertionError("expected ValueError on missing Behavior section")
