"""Outbound provenance invariant (2026-06-10, intent-first dispatch rebuild).

Two halves:

1. RUNTIME: the canonical send wrapper REFUSES (blocked result, loud log,
   nothing reaches BlueBubbles) any send without valid provenance —
   either {"llm_call": "<call_type>"} or {"fallback_audit": "YYYY-MM-DD"}.
   Valid provenance is stamped onto the outbound eval row.

2. STATIC SCAN (architectural, name-pattern style — ungameable): every
   production call site of the send wrappers carries an explicit
   `provenance=` kwarg. A template send can no longer slip in untagged.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_send_provenance.py -v
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers

from kavi_runtime.runtime import outbound_scanner as _outbound_scanner

# Test-owned fictional household handles. Pinned here so these tests do not
# depend on the real handles the runtime allowlist is configured with.
_TEST_HOUSEHOLD_HANDLES = {
    "+15555550101", "+15555550102", "megha@example.com", "max@example.com",
}


@pytest.fixture(autouse=True)
def _test_household_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _outbound_scanner, "is_household_handle",
        lambda h: bool(h) and h.strip().lower() in _TEST_HOUSEHOLD_HANDLES,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
            "chat_guid_prefix": "any;-;",
        },
        "graph": {"mstodo_shared_list_id": "AQMkADAwTEST=="},
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "enable_prompt_caching": False},
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "inbound.jsonl"),
        },
    }


def _wire_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    graph = MagicMock()
    claude = MagicMock()
    bb = MagicMock()
    bb.send_with_verify.return_value = {
        "sent": True, "verified": True,
        "temp_guid": "fake-guid", "message_guid": "real-guid", "send_response": {},
    }
    _stub = lambda cfg: (graph, claude, bb)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)
    return {"graph": graph, "claude": claude, "bb": bb}


# ---- runtime refusal --------------------------------------------------------


def test_untagged_send_is_blocked(monkeypatch, tmp_path) -> None:
    """No provenance kwarg at all → refused before any client/gate work."""
    cfg = _config(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handlers._send_imessage_with_fallback(
        cfg, "Hello", kind="test_untagged",
    )
    assert result["blocked"] is True
    assert result["blocked_reason"] == "missing_provenance"
    assert result["sent"] is False
    mocks["bb"].send_with_verify.assert_not_called()


@pytest.mark.parametrize("bad_provenance,reason", [
    ({"llm_call": ""}, "invalid_provenance_llm_call"),
    ({"fallback_audit": "yesterday"}, "invalid_provenance_fallback_audit"),
    ({"llm_call": "x", "fallback_audit": "2026-06-10"}, "invalid_provenance_shape"),
    ({}, "invalid_provenance_shape"),
    ("compose_conversational", "missing_provenance"),
])
def test_invalid_provenance_shapes_are_blocked(
    monkeypatch, tmp_path, bad_provenance, reason,
) -> None:
    cfg = _config(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handlers._send_imessage_with_fallback(
        cfg, "Hello", kind="test_bad_prov", provenance=bad_provenance,
    )
    assert result["blocked"] is True
    assert result["blocked_reason"] == reason
    mocks["bb"].send_with_verify.assert_not_called()


@pytest.mark.parametrize("good_provenance", [
    {"llm_call": "compose_kavi_reply"},
    {"fallback_audit": "2026-06-10"},
])
def test_valid_provenance_sends_and_stamps_eval_row(
    monkeypatch, tmp_path, good_provenance,
) -> None:
    """Valid provenance → the send proceeds AND the outbound eval row
    carries the provenance in its context."""
    cfg = _config(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handlers._send_imessage_with_fallback(
        cfg, "All clear today.", kind="periodic_summary",
        provenance=good_provenance,
    )
    assert result["blocked"] is False
    assert result["sent"] is True
    mocks["bb"].send_with_verify.assert_called_once()

    rows = [
        json.loads(l)
        for l in Path(cfg["paths"]["eval_persona_outbound_judgments_jsonl"])
        .read_text().splitlines() if l.strip()
    ]
    assert rows, "outbound eval row must exist"
    assert rows[-1]["context"]["provenance"] == good_provenance


def test_send_imessage_raw_self_stamps_fallback_audit(monkeypatch, tmp_path) -> None:
    """The degraded-mode raw sender stamps its own audited provenance —
    its callers (handler_alerts) need no tag."""
    cfg = _config(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handlers.send_imessage_raw(
        cfg, "Degraded right now.", recipient_handle="+15555550101",
    )
    assert result["blocked"] is False
    mocks["bb"].send_with_verify.assert_called_once()


# ---- static call-site scan --------------------------------------------------

_WRAPPER_NAMES = {
    "_send_imessage_with_fallback",
    "_send_imessage_with_fallback_and_context",
    "_send_with_scanner",
}

# Two layouts (Verifier FAIL 2026-06-11 07:49 PT): dev checkout has
# kavi-runtime/kavi_runtime + repo-root capabilities/; the DEPLOYED tree on
# Kavi is flat — kavi_runtime/ and capabilities/ are siblings under
# /Users/kavi/kavi-runtime. The old dev-only paths resolved capabilities to
# a nonexistent dir on Kavi, silently scanning 10/30 call sites (the
# >= 15 sanity assert below is what caught it). Resolve per layout; the
# sanity floor stays the backstop against both candidates missing.
_RUNTIME_DIR = Path(__file__).resolve().parent.parent  # .../kavi-runtime (dev) or deploy root
_SCAN_DIRS = [
    _RUNTIME_DIR / "kavi_runtime",
    next(
        (p for p in (
            _RUNTIME_DIR / "capabilities",          # deployed layout
            _RUNTIME_DIR.parent / "capabilities",   # dev checkout layout
        ) if p.is_dir()),
        _RUNTIME_DIR / "capabilities",
    ),
]


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _iter_production_files():
    for root in _SCAN_DIRS:
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts or "tests" in p.parts:
                continue
            yield p


def test_every_send_call_site_carries_provenance() -> None:
    """Architectural scan: every production call of a send wrapper passes
    an explicit `provenance=` keyword (or splats **kwargs through, which
    the wrapper's own runtime refusal still guards)."""
    violations: list[str] = []
    for path in _iter_production_files():
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in _WRAPPER_NAMES:
                continue
            kw_names = {kw.arg for kw in node.keywords}
            has_provenance = "provenance" in kw_names or None in kw_names
            if not has_provenance:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} calls "
                    f"{name} without provenance="
                )
    assert not violations, (
        "Send call sites missing the provenance kwarg (2026-06-10 outbound "
        "provenance invariant — tag with {'llm_call': ...} or "
        "{'fallback_audit': 'YYYY-MM-DD'}):\n  " + "\n  ".join(violations)
    )


def test_scan_actually_sees_call_sites() -> None:
    """Self-check that the scanner isn't trivially green: it must find a
    meaningful number of wrapper call sites across the codebase."""
    count = 0
    for path in _iter_production_files():
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in _WRAPPER_NAMES:
                count += 1
    assert count >= 15, f"scanner found only {count} call sites — broken scan?"
