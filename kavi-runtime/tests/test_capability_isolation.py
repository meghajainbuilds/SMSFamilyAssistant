"""Architectural test: per-capability directory isolation.

For each `capabilities/<name>/` directory, walk every .py file and assert
its imports come from one of:

- Standard library (sys, json, re, etc.)
- Third-party packages (anthropic, fastapi, etc.)
- The same capability's own directory (`capabilities.<name>.*`)
- The runtime cross-cutting alias (`kavi_runtime.runtime` or
  `kavi_runtime.*`) — these are explicitly allowed because cross-cutting
  code is the canonical home for shared plumbing.

NOT allowed: imports from another capability's directory. If
`capabilities.kavi_persona.foo` imports from `capabilities.inbox_to_task.bar`
the test fails — two capabilities reaching into each other is the exact
drift the directory split is meant to prevent.

Added 2026-06-02 as part of the architectural refactor (Phase 4 +
architectural-tests block).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CAPABILITIES_DIR = REPO_ROOT / "capabilities"

# Sub-directories of capabilities/ that are NOT capability roots
# (registry, templates, etc.).
NON_CAPABILITY_DIRS: set[str] = {"__pycache__"}


def _capability_dirs() -> list[Path]:
    """Return the per-capability directory roots."""
    return [
        p
        for p in CAPABILITIES_DIR.iterdir()
        if p.is_dir() and p.name not in NON_CAPABILITY_DIRS and not p.name.startswith(".")
    ]


def _collect_imports(py_file: Path) -> set[str]:
    """Return the set of top-level module names imported by this file."""
    try:
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module)
    return out


def _is_foreign_capability_import(
    module: str, own_capability: str
) -> bool:
    """Returns True iff `module` imports from a DIFFERENT capability's dir."""
    if not module.startswith("capabilities."):
        return False
    parts = module.split(".")
    if len(parts) < 2:
        return False
    target_capability = parts[1]
    if target_capability == own_capability:
        return False
    if target_capability in NON_CAPABILITY_DIRS:
        return False
    return True


def test_no_cross_capability_imports():
    """Each capability directory must not import from a sibling capability."""
    capability_dirs = _capability_dirs()
    assert capability_dirs, (
        f"No capability dirs found under {CAPABILITIES_DIR}. "
        "Phase 4 of the architectural refactor should have created them."
    )

    violations: list[tuple[str, str, str]] = []  # (file, own_cap, foreign_module)
    for cap_dir in capability_dirs:
        own_cap = cap_dir.name
        for py_file in cap_dir.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            for module in _collect_imports(py_file):
                if _is_foreign_capability_import(module, own_cap):
                    violations.append((
                        str(py_file.relative_to(REPO_ROOT)),
                        own_cap,
                        module,
                    ))

    assert not violations, (
        "Cross-capability imports detected (forbidden by Phase 4 of the "
        "architectural refactor):\n"
        + "\n".join(
            f"  {f} (in {own_cap}) imports from {module}"
            for f, own_cap, module in violations
        )
    )


def test_every_capability_dir_has_required_files():
    """Each capability directory must expose the canonical entry-point files
    so an Investigator opening the dir finds the axis-by-axis map. Required:
    `__init__.py`, `README.md`, `spec.md`."""
    capability_dirs = _capability_dirs()
    required = {"__init__.py", "README.md", "spec.md"}

    missing: list[tuple[str, str]] = []
    for cap_dir in capability_dirs:
        present = {p.name for p in cap_dir.iterdir() if p.is_file()}
        for r in required:
            if r not in present:
                missing.append((cap_dir.name, r))

    assert not missing, (
        "Capability directories missing required entry-point files:\n"
        + "\n".join(
            f"  capabilities/{cap}/ is missing {filename}"
            for cap, filename in missing
        )
    )
