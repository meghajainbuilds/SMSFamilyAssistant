"""Architectural test: deep verify parity across LLM-shaped capabilities.

For every capability in `capabilities/_role_registry.md` whose
`capability_type` includes any of {generative, judgment, agentic, two-way,
retrieval}, this test asserts:

1. The registry row's `verifier_procedure` column starts with `deep:` (not
   `shallow:`).
2. A `capabilities/<name>/verify.py` file exists.
3. That file's content names both shape gates AND selection gates (or
   equivalent gate-category markers). Shape-only verifiers caught Phase 1's
   ghost-spec residue but missed the selection-behavior class.

Added 2026-06-02 as part of the Phase 6 of the architectural refactor.

The test is intentionally text-based on the registry markdown (not a
Python-importable registry object) — that keeps the registry as the single
source of truth without forcing a parallel data structure that could drift.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY_PATH = REPO_ROOT / "capabilities" / "_role_registry.md"
CAPABILITIES_DIR = REPO_ROOT / "capabilities"

LLM_SHAPED_TYPES = {"generative", "judgment", "agentic", "two-way", "retrieval"}

# Map registry-row capability slug (kebab-case) to filesystem dir
# (snake_case). Phase 4 chose snake_case for the dir names because Python
# packages can't have hyphens.
SLUG_TO_DIR: dict[str, str] = {
    "kavi-persona": "kavi_persona",
    "inbox-to-task": "inbox_to_task",
    "realtime-kavi": "realtime_kavi",
    "kavi-coordinates": "coordination",
}


def _parse_registry_rows() -> list[dict[str, str]]:
    """Parse the registry table into a list of dicts with capability,
    capability_type (best-effort from the rationale column), and
    verifier_procedure."""
    if not REGISTRY_PATH.exists():
        return []
    rows: list[dict[str, str]] = []
    in_table = False
    for line in REGISTRY_PATH.read_text().splitlines():
        # Find the registry table block.
        if line.startswith("| capability |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and not line.startswith("|"):
            in_table = False
            continue
        if in_table and line.startswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) < 5:
                continue
            cap_raw = cells[0].strip("` ")
            rows.append({
                "capability": cap_raw,
                "investigator_log_path": cells[1],
                "live_state_source": cells[2],
                "verifier_procedure": cells[3],
                "rationale": cells[4],
            })
    return rows


def _capability_types_from_rationale(rationale: str) -> set[str]:
    """Best-effort extraction of capability_type from the rationale column.
    The rationale typically reads `LLM-shaped (capability_type: [generative,
    two-way]). ...`. We scan for the type names from LLM_SHAPED_TYPES plus
    {runtime, meta}."""
    candidates = {"generative", "judgment", "agentic", "two-way",
                  "retrieval", "runtime", "meta"}
    out: set[str] = set()
    for name in candidates:
        if re.search(rf"\b{re.escape(name)}\b", rationale):
            out.add(name)
    return out


def test_llm_shaped_capabilities_have_deep_verify():
    """Every LLM-shaped registry row must declare `deep:` in its
    verifier_procedure column."""
    rows = _parse_registry_rows()
    assert rows, (
        f"No registry rows parsed from {REGISTRY_PATH}. "
        "The architectural test requires the registry table to be parseable."
    )

    violations: list[str] = []
    for row in rows:
        cap = row["capability"]
        types = _capability_types_from_rationale(row["rationale"])
        if not types & LLM_SHAPED_TYPES:
            continue
        # Strip leading backticks for backtick-quoted procedure names.
        procedure_text = row["verifier_procedure"].lstrip("` ")
        if not procedure_text.startswith("deep:"):
            violations.append(
                f"{cap} (types {sorted(types)}) has "
                f"verifier_procedure={row['verifier_procedure']!r} but "
                f"LLM-shaped capabilities require deep:"
            )

    assert not violations, (
        "Registry rows missing deep verify for LLM-shaped capabilities:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_llm_shaped_capabilities_have_verify_py_with_both_gate_categories():
    """For every LLM-shaped capability, capabilities/<dir>/verify.py must
    exist AND name both shape-gate and selection-gate markers."""
    rows = _parse_registry_rows()
    violations: list[str] = []

    for row in rows:
        cap = row["capability"]
        types = _capability_types_from_rationale(row["rationale"])
        if not types & LLM_SHAPED_TYPES:
            continue

        dir_name = SLUG_TO_DIR.get(cap)
        if not dir_name:
            violations.append(
                f"{cap}: no SLUG_TO_DIR mapping in this test. Add the "
                f"capability to SLUG_TO_DIR or rename the capabilities/ dir "
                f"to match."
            )
            continue

        verify_path = CAPABILITIES_DIR / dir_name / "verify.py"
        if not verify_path.exists():
            violations.append(
                f"{cap}: capabilities/{dir_name}/verify.py is missing. "
                f"LLM-shaped capabilities require a deep verify entry point."
            )
            continue

        content = verify_path.read_text().lower()
        # Look for both gate-category markers (shape and selection).
        has_shape = (
            "shape" in content or "shape_gates" in content
            or "structural" in content
        )
        has_selection = (
            "selection" in content or "selection_gates" in content
            or "expected_" in content  # the inbox-to-task gate naming style
        )
        if not has_shape:
            violations.append(
                f"{cap}: capabilities/{dir_name}/verify.py does NOT name a "
                f"shape-gate category (expected 'shape' or 'structural')."
            )
        if not has_selection:
            violations.append(
                f"{cap}: capabilities/{dir_name}/verify.py does NOT name a "
                f"selection-gate category (expected 'selection' or "
                f"'expected_').  Phase 6 deep verify requires both."
            )

    assert not violations, (
        "Deep verify parity check failed:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
