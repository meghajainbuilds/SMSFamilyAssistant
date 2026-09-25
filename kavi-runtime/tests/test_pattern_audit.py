"""Smoke tests for scripts/pattern_audit.py.

Asserts the script runs against the real codebase and surfaces the
canonical anti-patterns named in the 2026-05-08 post-mortem:
  - claude_client.py [:30] / [:100] slices
  - claude_client.py max_tokens=1500 literal
  - graph_client.py $top=<int> literals
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
RUNTIME_PKG = Path(__file__).resolve().parents[1] / "kavi_runtime"
# Phase 4 (2026-06-02): claude_client.py methods physically moved to
# capabilities/kavi_persona/. Pattern audit must scan both.
CAPABILITIES_DIR = Path(__file__).resolve().parents[2] / "capabilities"


def _load_audit_module():
    """Import the script as a module without executing main(). Registers
    it in sys.modules so dataclass introspection (which looks up
    __module__ in sys.modules) works correctly."""
    if "pattern_audit" in sys.modules:
        return sys.modules["pattern_audit"]
    spec = importlib.util.spec_from_file_location(
        "pattern_audit", SCRIPTS_DIR / "pattern_audit.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pattern_audit"] = module
    spec.loader.exec_module(module)
    return module


def test_pattern_audit_script_imports() -> None:
    mod = _load_audit_module()
    assert hasattr(mod, "run_audit")
    assert hasattr(mod, "main")


def test_pattern_audit_finds_max_tokens_literals_in_capability_composers() -> None:
    """Phase 4 (2026-06-02): claude_client.py method bodies moved to
    capabilities/. Audit must surface the 1500 literal in its new home."""
    mod = _load_audit_module()
    matches = mod.find_hardcoded_max_tokens(RUNTIME_PKG)
    matches += mod.find_hardcoded_max_tokens(CAPABILITIES_DIR)
    # The 1500-cap from 6015776 is the canonical post-fix value.
    cap_1500 = [m for m in matches if "1500" in m.text]
    assert cap_1500, "expected max_tokens=1500 literal somewhere in runtime + capabilities"


def test_pattern_audit_finds_slice_caps_in_capability_composers() -> None:
    """Phase 4 (2026-06-02): slice-cap matches now live in capability composers."""
    mod = _load_audit_module()
    matches = mod.find_slices_upstream_of_llm_calls(RUNTIME_PKG)
    matches += mod.find_slices_upstream_of_llm_calls(CAPABILITIES_DIR)
    texts = " ".join(m.text for m in matches)
    assert "[:100]" in texts, "expected [:100] slice somewhere in runtime + capabilities"
    assert "[:30]" in texts, "expected [:30] slice somewhere in runtime + capabilities"


def test_pattern_audit_finds_graph_top_literals() -> None:
    mod = _load_audit_module()
    matches = mod.find_hardcoded_graph_top(RUNTIME_PKG)
    files = {m.file for m in matches}
    assert any("graph_client.py" in f for f in files), (
        f"graph $top audit should find graph_client.py literals; got {files}"
    )


def test_pattern_audit_render_report_well_formed() -> None:
    """Full run produces a markdown report with the 6 expected sections."""
    mod = _load_audit_module()
    report = mod.run_audit(RUNTIME_PKG)
    assert report.startswith("# Pattern audit report")
    for section_title in (
        "Slice caps before LLM calls",
        "Regex filters between LLM calls",
        "Hardcoded max_tokens literals",
        "Hardcoded timeout literals",
        "Hardcoded Graph paging caps",
        "Bare except / except-pass",
    ):
        assert f"## {section_title}" in report, (
            f"section missing from report: {section_title}"
        )
