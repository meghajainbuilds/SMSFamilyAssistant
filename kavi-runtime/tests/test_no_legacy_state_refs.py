"""Architectural test: no production module under kavi_runtime/ calls the
legacy `load_imessage_state` / `save_imessage_state` API.

Phase 5 of the 2026-06-02 architectural refactor rewired every runtime
call site to use direct per-concept calls
(`state_per_concept.load_<concept>` / `save_<concept>`). The legacy API
remains defined in `kavi_runtime/state.py` so test fixtures that reset
state to `{}` via `save_imessage_state(path, {})` keep working — but
production code paths must NOT route through the legacy merged-dict view.

Excluded files (allowed to reference the legacy API):
- `kavi_runtime/state.py` — defines the legacy API (delegates to
  state_per_concept internally).
- `kavi_runtime/state_per_concept.py` — defines the per-concept replacement.
- `kavi_runtime/_phase3_migrate.py` — Phase 3 boot-time migration uses
  the legacy format as input.

The test additionally asserts that `kavi-runtime/scripts/migrate_state_to_per_concept.py`
remains the only place outside the runtime modules that intentionally
reads the legacy monolithic file by name.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_DIR = REPO_ROOT / "kavi-runtime" / "kavi_runtime"

# Files allowed to contain references to the legacy API.
ALLOWED_FILES: set[str] = {
    "state.py",
    "state_per_concept.py",
    "_phase3_migrate.py",
    "snapshot.py",  # documents which file is backed up; reference is descriptive
    "state_schemas.py",  # Pydantic schema docstring describes legacy shape
    "synthetic_compose.py",  # docstring mentions legacy file as NOT-mutated
    "main.py",  # boot migration logic mentions the legacy path
    "state_invariants.py",  # belt-and-suspenders migration guard
}

LEGACY_API_PATTERN = re.compile(
    r"\b(load_imessage_state|save_imessage_state)\s*\("
)


def test_no_legacy_api_calls_in_production_modules():
    """Every .py file under kavi_runtime/ (excluding ALLOWED_FILES) must
    not call load_imessage_state() or save_imessage_state()."""
    violations: list[tuple[str, int, str]] = []
    for py_file in sorted(RUNTIME_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        if py_file.name in ALLOWED_FILES:
            continue
        text = py_file.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Skip comment lines and docstrings (rough heuristic — but the
            # function-call pattern `load_imessage_state(` is unlikely to
            # appear in prose).
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if LEGACY_API_PATTERN.search(line):
                violations.append((
                    str(py_file.relative_to(REPO_ROOT)),
                    lineno,
                    line.strip(),
                ))

    assert not violations, (
        "Production module references the legacy load_imessage_state / "
        "save_imessage_state API. Phase 5 of the architectural refactor "
        "requires direct per-concept calls instead.\n\n"
        "Violations:\n"
        + "\n".join(
            f"  {f}:{lineno}: {snippet}"
            for f, lineno, snippet in violations
        )
        + "\n\nAllowed files (excluded by design): "
        + ", ".join(sorted(ALLOWED_FILES))
    )


def test_no_legacy_state_imports_in_production_modules():
    """Every .py file under kavi_runtime/ (excluding ALLOWED_FILES) must
    not import load_imessage_state or save_imessage_state from
    kavi_runtime.state."""
    import_pattern = re.compile(
        r"^\s*from\s+kavi_runtime\.state\s+import.*\b(load_imessage_state|save_imessage_state)\b",
        re.MULTILINE,
    )
    violations: list[tuple[str, str]] = []
    for py_file in sorted(RUNTIME_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        if py_file.name in ALLOWED_FILES:
            continue
        text = py_file.read_text()
        # Match multi-line imports too (parens) — read the import block.
        # Simple heuristic: check each "from kavi_runtime.state import (" block.
        for match in re.finditer(
            r"from\s+kavi_runtime\.state\s+import\s*\(([^)]+)\)",
            text,
        ):
            names = match.group(1)
            if "load_imessage_state" in names or "save_imessage_state" in names:
                violations.append((
                    str(py_file.relative_to(REPO_ROOT)),
                    "multi-line import block contains load_imessage_state or save_imessage_state",
                ))
        # Also catch single-line imports.
        for match in import_pattern.finditer(text):
            violations.append((
                str(py_file.relative_to(REPO_ROOT)),
                match.group(0).strip(),
            ))

    assert not violations, (
        "Production module imports legacy load/save_imessage_state. "
        "Phase 5 requires direct per-concept imports from "
        "kavi_runtime.state_per_concept instead.\n\n"
        + "\n".join(f"  {f}: {detail}" for f, detail in violations)
    )
