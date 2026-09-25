"""test_no_unbound_globals.py — architectural test: every global name a
function body references must be bound in THAT module's namespace.

Why: twice now (2026-05-07, 2026-06-10) a physical code move relocated a
function's call site and its definition into different modules without an
import bridging them. Import-time checks pass, the test suite passes
(nothing executes the branch), and the first real message to exercise the
path crashes with NameError — Kavi texts Megha the degraded fallback. The
2026-06-10 instance: `_apply_qa_resolutions` / `_handle_qa_reply` called
from kavi_runtime/runtime/imessage_dispatch.py but defined (and only
star-imported elsewhere) in capabilities/kavi_persona/qa_loop/handler.py.
The prior guard (test_handlers_imports.py) asserted names on the handlers
module — the wrong namespace after the Phase 4 move — and stayed green.

Mechanism: import each module, walk every function's bytecode (including
nested code objects) for LOAD_GLOBAL names, and assert each name resolves
in the module's globals or builtins. Names bound lazily inside the
function (local imports, parameters, assignments) never compile to
LOAD_GLOBAL, so they don't false-positive.
"""

from __future__ import annotations

import builtins
import dis
import importlib
import inspect
import pkgutil
import types

import pytest

# Modules under guard: the runtime package + per-capability code. Anything
# importable under these roots gets scanned.
SCAN_ROOTS = ["kavi_runtime", "capabilities"]

# Modules that cannot import in CI. Add a module here ONLY with a dated
# comment explaining why; an entry without a comment should fail review.
IMPORT_SKIP: set[str] = {
    # 2026-06-10: imports uvicorn, which is a deploy-only dependency (the
    # dev venv runs tests, not the server). Nothing in main.py is a moved
    # function body; it is the boot entrypoint.
    "kavi_runtime.main",
}

# Production boots main -> server with kavi_runtime.handlers fully
# initialized before any capabilities.realtime_kavi module loads. The
# parametrized sweep below imports modules alphabetically, which can hit a
# circular partial-init (capabilities.realtime_kavi.* -> kavi_runtime.
# handlers mid-init -> ImportError) that production never sees. Importing
# handlers first mirrors the production order.
importlib.import_module("kavi_runtime.handlers")


def _iter_module_names() -> list[str]:
    names: list[str] = []
    for root in SCAN_ROOTS:
        pkg = importlib.import_module(root)
        names.append(root)
        for info in pkgutil.walk_packages(pkg.__path__, prefix=f"{root}."):
            names.append(info.name)
    return sorted(set(names) - IMPORT_SKIP)


def _code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def _stored_globals(module: types.ModuleType) -> set[str]:
    """Names any function in the module assigns via `global` (STORE_GLOBAL /
    DELETE_GLOBAL). These are deliberate lazily-bound module globals (e.g.
    kavi_runtime.secrets._keyring_mod's cache-on-first-call pattern) — the
    binding appears at first call, not at import, so they are not the
    lost-import class."""
    stored: set[str] = set()
    for _, obj in inspect.getmembers(module):
        funcs: list[types.FunctionType] = []
        if isinstance(obj, types.FunctionType) and obj.__module__ == module.__name__:
            funcs.append(obj)
        elif isinstance(obj, type):
            funcs.extend(
                m for _, m in inspect.getmembers(obj)
                if isinstance(m, types.FunctionType) and m.__module__ == module.__name__
            )
        for func in funcs:
            for code in _code_objects(func.__code__):
                for instr in dis.get_instructions(code):
                    if instr.opname in ("STORE_GLOBAL", "DELETE_GLOBAL"):
                        stored.add(instr.argval)
    return stored


def _unbound_globals(module: types.ModuleType) -> list[str]:
    problems: list[str] = []
    seen: set[int] = set()
    lazily_bound = _stored_globals(module)
    for _, obj in inspect.getmembers(module):
        func = None
        if isinstance(obj, types.FunctionType):
            func = obj
        elif isinstance(obj, type):
            for _, meth in inspect.getmembers(obj):
                if isinstance(meth, types.FunctionType) and meth.__module__ == module.__name__:
                    for code in _code_objects(meth.__code__):
                        if id(code) in seen:
                            continue
                        seen.add(id(code))
                        problems.extend(_scan_code(code, module, lazily_bound))
            continue
        if func is None or func.__module__ != module.__name__:
            continue
        for code in _code_objects(func.__code__):
            if id(code) in seen:
                continue
            seen.add(id(code))
            problems.extend(_scan_code(code, module, lazily_bound))
    return problems


def _scan_code(
    code: types.CodeType, module: types.ModuleType, lazily_bound: set[str],
) -> list[str]:
    out = []
    for instr in dis.get_instructions(code):
        if instr.opname not in ("LOAD_GLOBAL", "LOAD_NAME"):
            continue
        name = instr.argval
        if name in module.__dict__ or hasattr(builtins, name) or name in lazily_bound:
            continue
        out.append(f"{module.__name__}.{code.co_qualname}: unbound global {name!r}")
    return out


@pytest.mark.parametrize("module_name", _iter_module_names())
def test_no_unbound_globals_in_runtime_modules(module_name: str) -> None:
    module = importlib.import_module(module_name)
    problems = _unbound_globals(module)
    assert not problems, (
        "Unbound global name(s) — a function references a name not bound in "
        "its own module (the 'physical move lost an import' class, "
        "2026-05-07 + 2026-06-10 incidents):\n  " + "\n  ".join(problems)
    )
