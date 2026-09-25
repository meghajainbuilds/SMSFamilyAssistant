"""Shared test plumbing for the regression library.

Each `test_<id>.py` file imports from here so the per-test bodies stay
focused on the assertion shape unique to that fixture. Mirrors the
helpers in tests/test_action_layer_llm_first_2026_05_08.py without
duplicating them — tests there are pre-Investment-1 and stay put for
historical reasons.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers


def make_config(tmp_path: Path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {"mstodo_shared_list_id": "AQMkADAwTEST=="},
        "action_layer": {"dry_run": False},
        "paths": {
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "eval_persona_action_intent_jsonl": str(tmp_path / "action_intent.jsonl"),
        },
    }


def patch_send_paths(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]],
) -> None:
    def _send_plain(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": None})
        return {"verified": True, "fallback_used": False}

    def _send_with_context(
        config: dict, text: str, *, kind: str, context: dict[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send_plain)
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context", _send_with_context,
    )
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)


def claude_with_action_responses(fx_responses: dict[str, Any]) -> MagicMock:
    """Mount a mocked ClaudeClient where each entry in
    `fx_responses["mock_llm_responses"]` becomes the return value of the
    matching method on the mock. Methods not listed remain unset; the test
    can configure them itself or a side_effect=AssertionError to assert
    they MUST NOT be called.
    """
    claude = MagicMock()
    for method, resp in (fx_responses or {}).items():
        getattr(claude, method).return_value = resp

    def _no_conv(*a, **kw):
        raise AssertionError(
            "compose_conversational_reply must NOT be called for action-implying inbounds"
        )

    claude.compose_conversational_reply.side_effect = _no_conv
    return claude
