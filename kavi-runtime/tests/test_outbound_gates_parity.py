"""Static-analysis parity test (B2 of the 2026-05-06 evening audit follow-up).

Asserts that the four leaf primitives that emit outbound content all run
through `outbound_scanner.gate_outbound_content` BEFORE the network call.
The four primitives:

  - GraphClient.create_todo_task        (email path)
  - GraphClient.create_task_in_shared_list (iMessage create-task verb,
                                           coordination 4b, manual missed)
  - GraphClient.send_mail               (Outlook fallback)
  - handlers._send_imessage_with_fallback (every Kavi outbound iMessage)

For each, walk the function body via `ast` and assert that
`gate_outbound_content` is referenced. This is a structural smoke test —
it does NOT verify the gate runs before the network call (only a runtime
test can do that). But it catches the most common regression pattern:
a future PR adds a new outbound path on the same primitive and forgets
to land the gate, OR a refactor inlines the gate's call site away.

Why static analysis on the leaf rather than every caller: callers are
many (and grow); leaves are few (and stable). After A1, every outbound
primitive carries the gate at the leaf, so the parity test only needs
to scan the leaf bodies.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_outbound_gates_parity.py -v
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from kavi_runtime import graph_client, handlers


def _function_body_calls(func) -> set[str]:
    """Return the set of function-call names (qualified or unqualified) made
    inside `func`. Walks the AST and pulls out any `name(...)` or
    `mod.attr(...)` invocation. Returns the dotted name (e.g.,
    `outbound_scanner.gate_outbound_content`).
    """
    try:
        src = inspect.getsource(func)
    except OSError:
        pytest.skip(f"could not read source for {func.__qualname__}")
    # `inspect.getsource` for a method returns the def with the class-level
    # indentation preserved; `textwrap.dedent` strips the common leading
    # whitespace so the AST parser sees a top-level function-def. (Using
    # `inspect.cleandoc` instead breaks on docstrings with their own
    # indentation conventions.)
    src = textwrap.dedent(src)
    tree = ast.parse(src)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                # Walk the chain back to the root Name.
                parts: list[str] = [f.attr]
                cur = f.value
                while isinstance(cur, ast.Attribute):
                    parts.insert(0, cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.insert(0, cur.id)
                out.add(".".join(parts))
    return out


def _assert_gate_called(func, label: str) -> None:
    calls = _function_body_calls(func)
    matches = [c for c in calls if "gate_outbound_content" in c]
    assert matches, (
        f"{label}: outbound primitive does not call gate_outbound_content. "
        f"Every leaf-level outbound MUST run the content scanner BEFORE the "
        f"network call. If you genuinely meant to bypass the gate (you didn't), "
        f"document that decision in the capability changelog and the spec's "
        f"Guardrails section, then add an explicit allowlist exception here."
    )


def test_create_todo_task_invokes_outbound_gate() -> None:
    _assert_gate_called(
        graph_client.GraphClient.create_todo_task,
        "GraphClient.create_todo_task",
    )


def test_create_task_in_shared_list_invokes_outbound_gate() -> None:
    _assert_gate_called(
        graph_client.GraphClient.create_task_in_shared_list,
        "GraphClient.create_task_in_shared_list",
    )


def test_send_mail_invokes_outbound_gate() -> None:
    _assert_gate_called(
        graph_client.GraphClient.send_mail,
        "GraphClient.send_mail",
    )


def test_send_imessage_with_fallback_invokes_outbound_gate() -> None:
    """The handler-side iMessage send wrapper. Lives in handlers.py so the
    test imports it from the handlers module. This wrapper is the single
    entry point every Kavi outbound iMessage flows through (per the
    handler-alerts and quiet-hours-drain comments in the source)."""
    fn = getattr(handlers, "_send_imessage_with_fallback", None)
    if fn is None:
        pytest.skip("_send_imessage_with_fallback not present in handlers")
    _assert_gate_called(fn, "handlers._send_imessage_with_fallback")
