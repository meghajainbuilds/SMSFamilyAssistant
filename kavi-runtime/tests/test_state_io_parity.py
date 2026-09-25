"""test_state_io_parity.py — static-analysis test asserting that NO module
outside state_io.py performs raw atomic-write file I/O.

Why: HomeOS hit two production silent-failure incidents (2026-05-05,
2026-05-06) where multiple modules each implemented their own
atomic-replace pattern with subtly different correctness. Yesterday's fix
patched ONE writer; today's incident hit a SECOND writer at a different
file path. The cure is one helper, used everywhere — and this test
enforces it.

If this test fails, it is not a bug in the test. A new module is doing
raw atomic-write file I/O instead of going through `kavi_runtime.state_io`.
Fix the call site by routing the write through `atomic_write_json`,
`atomic_write_text`, or `atomic_write_bytes`.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "kavi_runtime"

# Modules allowed to call os.replace, tempfile.mkstemp, etc. — i.e. the
# helper itself is the only place these primitives live.
ALLOWED_MODULES = {"state_io.py"}

# Calls forbidden anywhere outside state_io.py. Each entry is (module, attr).
FORBIDDEN_CALLS = {
    ("os", "replace"),
    ("os", "rename"),
    ("tempfile", "mkstemp"),
    ("tempfile", "NamedTemporaryFile"),
}


def _is_module_attr_call(call: ast.Call, module: str, name: str) -> bool:
    fn = call.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr == name
        and isinstance(fn.value, ast.Name)
        and fn.value.id == module
    )


def _is_path_write_text_with_json_dumps(call: ast.Call) -> bool:
    """Detect `<some_path>.write_text(json.dumps(...))` and similar.

    Crude detection by source-text inspection of the argument: cheap and
    effective at catching the exact pattern that caused today's incident
    (token cache, subscription state, secrets fallback all used this).
    """
    fn = call.func
    if not (isinstance(fn, ast.Attribute) and fn.attr == "write_text"):
        return False
    for arg in call.args:
        try:
            if "json.dumps" in ast.unparse(arg):
                return True
        except AttributeError:
            return False
    return False


def test_no_raw_atomic_writes_outside_state_io() -> None:
    violations: list[str] = []
    for py in ROOT.rglob("*.py"):
        if py.name in ALLOWED_MODULES:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for module, name in FORBIDDEN_CALLS:
                if _is_module_attr_call(node, module, name):
                    violations.append(
                        f"{py.relative_to(ROOT)}:{node.lineno} calls "
                        f"{module}.{name}() — use kavi_runtime.state_io instead"
                    )
            if _is_path_write_text_with_json_dumps(node):
                violations.append(
                    f"{py.relative_to(ROOT)}:{node.lineno} writes JSON via "
                    f"<path>.write_text(json.dumps(...)) — use "
                    f"state_io.atomic_write_json instead"
                )
    assert not violations, (
        "Modules outside state_io.py are doing raw atomic file I/O. "
        "Yesterday's silent-failure incident was caused by exactly this "
        "pattern. Fix each call site:\n  " + "\n  ".join(violations)
    )
