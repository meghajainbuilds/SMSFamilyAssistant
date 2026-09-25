"""Regression test: `kavi_runtime/persona_prompts.py` was deleted on
2026-05-28 as part of the security-baseline spec collapse. The single
constant it held (`PERSONA_REFUSAL_LAYER`) moved into
`capabilities/security-baseline.md` and is now loaded at runtime by
`kavi_runtime/security_baseline.py`.

This test guards against the file being re-added by mistake and against
any Python file in the runtime quietly re-importing from it (which
would crash on the missing module — better to fail in the test suite
than at runtime on Kavi's Mac).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_persona_prompts_module_removed.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_PERSONA_PROMPTS_PATH = _RUNTIME_ROOT / "kavi_runtime" / "persona_prompts.py"

# Match both `from kavi_runtime.persona_prompts import ...` and
# `import kavi_runtime.persona_prompts` forms.
_FORBIDDEN_IMPORT = re.compile(
    r"^\s*(from\s+kavi_runtime\.persona_prompts\b|"
    r"import\s+kavi_runtime\.persona_prompts\b)",
    re.MULTILINE,
)


def test_persona_prompts_file_does_not_exist() -> None:
    """The file was removed on 2026-05-28. Re-adding it without the
    accompanying spec migration would re-create the spec-vs-runtime
    drift class of bug this test exists to prevent."""
    assert not _PERSONA_PROMPTS_PATH.exists(), (
        f"{_PERSONA_PROMPTS_PATH} was deleted on 2026-05-28. "
        "Security baseline text now lives in "
        "capabilities/security-baseline.md and is loaded at runtime "
        "by kavi_runtime/security_baseline.py. Do not re-add this file."
    )


def test_no_python_file_imports_persona_prompts() -> None:
    """Any `from kavi_runtime.persona_prompts import ...` or
    `import kavi_runtime.persona_prompts` line in any .py file under
    kavi-runtime/ would crash with ModuleNotFoundError at import time.
    This test catches stragglers in the test suite instead."""
    offenders: list[str] = []
    for py in sorted(_RUNTIME_ROOT.rglob("*.py")):
        # Skip this very test file — it talks ABOUT persona_prompts in
        # docstrings + this regex pattern but never imports it.
        if py.name == "test_persona_prompts_module_removed.py":
            continue
        body = py.read_text()
        if _FORBIDDEN_IMPORT.search(body):
            offenders.append(str(py.relative_to(_RUNTIME_ROOT)))
    assert not offenders, (
        "Python files still import from the removed "
        "kavi_runtime.persona_prompts module. Update them to use "
        "kavi_runtime.security_baseline.load_security_baseline_text "
        "instead.\n\nOffenders:\n  - " + "\n  - ".join(offenders)
    )
