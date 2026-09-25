"""Architectural test: no chat-mirror poll calls inside the verify flow.

Added 2026-06-03 as the anti-gaming structural property of the
receipt-as-truth verify rewrite. Locks the property: no callsite of
`fetch_recent_messages` (or any equivalent BlueBubbles chat-history
poll) may appear inside `send_with_verify`, `verify_send_landed`, or
the Outlook-fallback decision path in `runtime/send_imessage.py`.

Why this test is name-pattern-based against the AST:

The bug that caused the 2026-06-02 Max-bound false-negatives WAS that
`verify_send_landed` polled `fetch_recent_messages` against a chat
that BlueBubbles' chat.db had never mirrored for the recipient. The
fix (Layer A) deletes that polling path entirely. Without an
architectural test, a future engineer (human or otherwise) facing a
flaky per-message verification could re-add the poll "as a
backstop" — re-introducing the exact bug.

**The poll path stays available** for legitimate callers (`bb_heartbeat`
on Megha's chat for chat.db warmup; `channel_heartbeat.run_daily_
channel_heartbeat` for per-recipient round-trip health). Those callers
are NOT in the verify flow and are explicitly excluded.

**Allowlisting is forbidden.** If a future change wants `send_with_verify`
or `verify_send_landed` to call into the chat-poll path again, the right
move is to delete the call, not to extend an allowlist on this test.
The 2026-06-02 handoff documented this exact failure mode for prior
agent runs.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_no_chat_poll_verify.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BB_CLIENT = REPO_ROOT / "kavi_runtime" / "bluebubbles_client.py"
SEND_IMESSAGE = REPO_ROOT / "kavi_runtime" / "runtime" / "send_imessage.py"


# Function names that, if called from inside one of the forbidden regions,
# count as a chat-mirror poll regression. Keep this list small and tied to
# the actual API surface — adding entries means a NEW poll path has been
# invented and the same scrutiny applies.
FORBIDDEN_POLL_CALLS: set[str] = {
    "fetch_recent_messages",   # BlueBubbles /api/v1/message/query
    "fetch_message_by_guid",   # the deleted GUID-poll helper; readd would re-bug
}


# Forbidden regions in `bluebubbles_client.py` — these are the per-message
# verification entry points and any helper that participates in the verify
# decision.
FORBIDDEN_FUNCTIONS_IN_BB_CLIENT: set[str] = {
    "verify_send_landed",
    "send_with_verify",
}


# Forbidden regions in `runtime/send_imessage.py` — the Outlook-fallback
# decision path. The fallback must fire on send-call failure only; if it
# starts gating on chat-mirror state, the 2026-06-02 false-negative class
# returns.
FORBIDDEN_FUNCTIONS_IN_SEND_IMESSAGE: set[str] = {
    "_send_imessage_with_fallback",
    "_send_imessage_with_fallback_and_context",
    "send_imessage_raw",
}


def _collect_calls_in_function(node: ast.FunctionDef) -> list[tuple[str, int]]:
    """Return [(callee_name, lineno), ...] for every Call node inside this
    function body. Supports both bare names (`fetch_recent_messages()`)
    and attribute access (`self.fetch_recent_messages()`, `bb.
    fetch_recent_messages()`)."""
    calls: list[tuple[str, int]] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            calls.append((func.id, sub.lineno))
        elif isinstance(func, ast.Attribute):
            calls.append((func.attr, sub.lineno))
    return calls


def _scan_file_for_violations(
    path: Path, forbidden_funcs: set[str],
) -> list[tuple[str, str, int]]:
    """Walk `path`'s AST. For every function in `forbidden_funcs`, collect
    any call to a name in FORBIDDEN_POLL_CALLS. Return tuples of
    (containing_function, called_name, lineno)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    violations: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in forbidden_funcs:
            continue
        for callee, lineno in _collect_calls_in_function(node):
            if callee in FORBIDDEN_POLL_CALLS:
                violations.append((node.name, callee, lineno))
    return violations


def test_no_chat_poll_calls_in_bluebubbles_verify_functions() -> None:
    """`verify_send_landed` and `send_with_verify` must not call
    `fetch_recent_messages` or any equivalent chat-mirror poll. Per the
    2026-06-03 rewrite, the send response's GUID is the verification
    signal; polling chat.db produced false-negative silent-sends for any
    recipient whose chat had not been mirrored (every new household
    member, by construction)."""
    violations = _scan_file_for_violations(BB_CLIENT, FORBIDDEN_FUNCTIONS_IN_BB_CLIENT)
    assert not violations, _format_violations(
        violations,
        "kavi_runtime/bluebubbles_client.py",
        "verify functions must verify off the send-call response, not by polling chat history",
    )


def test_no_chat_poll_calls_in_send_imessage_fallback() -> None:
    """The Outlook-fallback decision path in `runtime/send_imessage.py` must
    not gate on chat-mirror reads. Fallback fires on send-call failure
    (HTTP non-2xx, network error, BlueBubbles unreachable, missing GUID
    in response). It must NOT consult chat history."""
    violations = _scan_file_for_violations(
        SEND_IMESSAGE, FORBIDDEN_FUNCTIONS_IN_SEND_IMESSAGE,
    )
    assert not violations, _format_violations(
        violations,
        "kavi_runtime/runtime/send_imessage.py",
        "Outlook fallback must fire on send-call failure only, not on chat-mirror state",
    )


def test_fetch_message_by_guid_deleted_from_module() -> None:
    """Belt-and-suspenders: the chat-poll-by-guid helper was deleted as
    part of the rewrite. If it returns to the module, the same false-
    negative class becomes reachable — verify must use the GUID returned
    by the send response, not a separate lookup."""
    src = BB_CLIENT.read_text()
    # Match a def line specifically; comments / docstrings referencing the
    # name are fine.
    import re
    matches = re.findall(r"^\s*def\s+fetch_message_by_guid\b", src, re.MULTILINE)
    assert not matches, (
        "fetch_message_by_guid was reintroduced in bluebubbles_client.py. "
        "That helper polls BlueBubbles for a GUID separately from the send "
        "response and reproduces the 2026-06-02 false-negative class for "
        "unmirrored chats. The send response already carries the GUID; use "
        "that directly in verify_send_landed instead."
    )


def test_bb_heartbeat_caller_remains_allowed() -> None:
    """Sanity: this test does NOT forbid `fetch_recent_messages` globally.
    `bb_heartbeat` (chat.db warmup on Megha's chat) is a legitimate caller
    outside the per-message verify flow and must keep working."""
    scheduler_path = REPO_ROOT.parent / "capabilities" / "realtime_kavi" / "scheduler.py"
    if scheduler_path.exists():
        src = scheduler_path.read_text()
        assert "fetch_recent_messages" in src, (
            "bb_heartbeat is expected to keep calling fetch_recent_messages "
            "for chat.db warmup. This test pins the contract that the poll "
            "API is still allowed OUTSIDE the verify flow."
        )


def test_channel_heartbeat_does_not_call_chat_poll() -> None:
    """Updated 2026-06-04 after the passive-observation rewrite.

    The original Layer B design (2026-06-03) sent a synthetic ping and
    polled chat history — exactly the broken signal class Layer A had
    just removed. The 08:00 PT cron on 2026-06-04 false-fired two
    "channel degrading" emails to Megha because the chat-history read
    for her own number returned 200 OK but didn't see the ping in the
    recent-50 window.

    The rewrite replaces the synthetic probe with passive observation
    of real outbound receipts + inbound webhook arrivals. `channel_
    heartbeat.py` must therefore NOT call `fetch_recent_messages` (or
    any other BlueBubbles chat-poll surface) — the daily check is now
    a pure read of state populated by write-through hooks.
    """
    hb_path = REPO_ROOT / "kavi_runtime" / "channel_heartbeat.py"
    assert hb_path.exists(), (
        "channel_heartbeat module is missing. The passive-observation "
        "daily channel health check lives at "
        "kavi_runtime/channel_heartbeat.py."
    )
    src = hb_path.read_text()
    assert "fetch_recent_messages" not in src, (
        "channel_heartbeat must NOT call fetch_recent_messages. The "
        "passive-observation rewrite (2026-06-04) replaced the synthetic "
        "ping + chat-history poll with write-through hooks on real "
        "outbound receipts and inbound webhook arrivals. If this string "
        "is back, a future engineer reintroduced the same false-negative "
        "class the receipt-as-truth verify rewrite (SHA af66b47) removed."
    )


def _format_violations(
    violations: list[tuple[str, str, int]], file_label: str, contract: str,
) -> str:
    lines = [f"\nForbidden chat-mirror poll call(s) inside the verify flow."]
    lines.append(f"Contract: {contract}")
    lines.append(f"File: {file_label}")
    lines.append("Violations:")
    for func_name, callee, lineno in violations:
        lines.append(f"  - {func_name}() calls {callee}() at line {lineno}")
    lines.append("")
    lines.append(
        "The fix is to DELETE the chat-poll call, not to add the function "
        "name to an allowlist here. The 2026-06-02 handoff documented this "
        "exact failure mode (allowlist-gaming) for prior agent runs."
    )
    return "\n".join(lines)
