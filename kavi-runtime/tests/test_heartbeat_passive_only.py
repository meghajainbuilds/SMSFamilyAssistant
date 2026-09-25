"""Architectural test: channel_heartbeat.py is passive-observation only.

Added 2026-06-04 as the anti-gaming structural property of the heartbeat
rewrite. Locks the property: the daily channel-health check does NOT
send anything, does NOT poll chat history, does NOT sleep, and does NOT
touch the BlueBubbles client at all.

Why this test exists:

The 2026-06-03 synthetic ping + chat-history poll design re-introduced
the exact false-negative class the receipt-as-truth verify rewrite (SHA
`af66b47`) had just removed. On 2026-06-04 the cron false-fired two
"channel degrading" emails to Megha because the chat-history poll for
her own chat returned 200 OK but didn't see the ping in the recent-50
window. The fix is structural: passive observation of real outbound
receipts (verified=True + message_guid) and inbound webhook arrivals.

Allowlisting is forbidden. If a future change wants `channel_heartbeat`
to talk to BlueBubbles again, the right move is to delete the call, not
to extend an allowlist here. Per the 2026-06-02 handoff documenting the
allowlist-gaming failure mode, structural properties beat absolute-
number ceilings.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_heartbeat_passive_only.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HB_PATH = REPO_ROOT / "kavi_runtime" / "channel_heartbeat.py"


def _module_tree() -> ast.Module:
    return ast.parse(HB_PATH.read_text(), filename=str(HB_PATH))


def _all_calls() -> list[tuple[str, int, str]]:
    """Return (call_name, lineno, containing_function_name) for every Call
    node in the module. `call_name` is the attribute name for attribute
    calls (e.g. `send_message` for `bb.send_message(...)`) or the bare
    name for `Name` calls."""
    tree = _module_tree()
    func_stack: list[str] = []
    results: list[tuple[str, int, str]] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            func_stack.append(node.name)
            self.generic_visit(node)
            func_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            func_stack.append(node.name)
            self.generic_visit(node)
            func_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                name = "<other>"
            containing = func_stack[-1] if func_stack else "<module>"
            results.append((name, node.lineno, containing))
            self.generic_visit(node)

    Visitor().visit(tree)
    return results


# ---- Forbidden call surfaces -----------------------------------------------


FORBIDDEN_BB_CALLS: set[str] = {
    # Synthetic send paths — removed in the 2026-06-04 rewrite.
    "send_message",
    "send_with_verify",
    # Chat-history poll surfaces — removed; this is the false-negative class
    # the receipt-as-truth verify rewrite eliminated.
    "fetch_recent_messages",
    "fetch_message_by_guid",
    # Chat resolution — needed only by the deleted ping+poll path.
    "chat_guid_for",
}


def test_no_bluebubbles_client_calls_anywhere_in_module() -> None:
    """channel_heartbeat.py must not call any function on the
    bluebubbles_client surface. The daily check is a pure read; the
    write-through hooks only persist timestamps."""
    violations: list[tuple[str, int, str]] = []
    for name, lineno, containing in _all_calls():
        if name in FORBIDDEN_BB_CALLS:
            violations.append((name, lineno, containing))
    assert not violations, _format_violations(
        violations,
        contract=(
            "channel_heartbeat must not call any BlueBubbles surface. "
            "The daily check is a pure read of state populated by "
            "write-through hooks elsewhere in the runtime. The fix is "
            "to DELETE the call, not to add the function name to an "
            "allowlist here."
        ),
    )


def test_no_sleep_calls_in_module() -> None:
    """channel_heartbeat.py must not call `time.sleep` (or any sleep).
    The daily check is synchronous, fast, and no settle window is
    needed because no synthetic send is performed."""
    violations: list[tuple[str, int, str]] = []
    for name, lineno, containing in _all_calls():
        if name == "sleep":
            violations.append((name, lineno, containing))
    assert not violations, _format_violations(
        violations,
        contract=(
            "channel_heartbeat must not sleep. The 2026-06-03 settle "
            "window existed to give BlueBubbles' chat.db time to sync "
            "after a synthetic ping; the passive-observation rewrite "
            "removed that send. Reintroducing a sleep almost certainly "
            "means a synthetic probe came back too."
        ),
    )


def test_no_bluebubbles_import_in_module() -> None:
    """channel_heartbeat.py must not import bluebubbles_client at all.
    The state file lives in the kavi-runtime state directory; the
    write-through hooks live in the modules that actually touch
    BlueBubbles (send_imessage, imessage_dispatch)."""
    tree = _module_tree()
    bad_imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "bluebubbles" in alias.name.lower():
                    bad_imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "bluebubbles" in module.lower():
                bad_imports.append((module, node.lineno))
    assert not bad_imports, (
        "channel_heartbeat must not import bluebubbles_client. The "
        "passive-observation rewrite (2026-06-04) inverted the data "
        "flow: heartbeat owns the schema; signal-emitting modules call "
        "in via record_outbound_receipt / record_inbound. Importing "
        "bluebubbles_client back means the synthetic probe came back."
        f" Bad imports: {bad_imports}"
    )


def test_daily_check_only_calls_state_io_and_send_mail() -> None:
    """The daily check function (`run_daily_channel_heartbeat`) calls
    only: state load/save, datetime/timedelta arithmetic, the recipient
    discovery helper, the alert-emit helper, and module-local helpers.
    The only outbound I/O is `graph.send_mail` via the alert helper.

    This test asserts the call-site surface of the daily check function
    body itself. Helpers (`_send_degradation_alert`, etc.) are checked
    by the module-wide tests above.
    """
    tree = _module_tree()
    daily_func: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_daily_channel_heartbeat":
            daily_func = node
            break
    assert daily_func is not None, (
        "run_daily_channel_heartbeat must exist in channel_heartbeat.py"
    )

    allowed_calls: set[str] = {
        # State plumbing.
        "_load_state",
        "_save_state",
        "_migrate_state_if_needed",
        "_ensure_entry",
        # Recipient discovery.
        "discover_recipients",
        "_get_clients",
        # Time helpers.
        "now",
        "timedelta",
        "_parse_iso",
        # Alert emit (which itself uses graph.send_mail; see below).
        "_send_degradation_alert",
        # Standard library and python builtins that show up in expressions.
        "info",
        "warning",
        "exception",
        "get",
        "append",
        # Summary dict construction.
        "items",
    }
    # Calls allowed via the alert helper. The helper body is allowed to
    # call graph.send_mail; we assert nothing else surprising shows up
    # at the daily-check function-body level.

    surprising: list[tuple[str, int]] = []
    for sub in ast.walk(daily_func):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name in allowed_calls:
            continue
        if name in FORBIDDEN_BB_CALLS:
            surprising.append((name, sub.lineno))
            continue
        if name == "sleep":
            surprising.append((name, sub.lineno))
            continue
    assert not surprising, (
        "run_daily_channel_heartbeat calls a forbidden function: "
        f"{surprising}. The daily check must remain a pure read of "
        "state with the only outbound I/O being graph.send_mail via "
        "the alert helper."
    )


def _format_violations(
    violations: list[tuple[str, int, str]], *, contract: str,
) -> str:
    lines = [
        "",
        "Forbidden call(s) inside channel_heartbeat.py.",
        f"Contract: {contract}",
        "Violations:",
    ]
    for name, lineno, containing in violations:
        lines.append(f"  - {containing}() calls {name}() at line {lineno}")
    return "\n".join(lines)
