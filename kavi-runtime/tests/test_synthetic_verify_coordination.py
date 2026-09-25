"""Deep verify gates for kavi-coordinates (Phase 0c closure, 2026-06-10).

The June 3 incident produced a week of "coordinating with Max on
something" digests — content-free output nobody could act on. The new
`/synthetic/verify/kavi-coordinates` surface (canonical gate logic in
`capabilities/coordination/verify.py`) rejects that class.

Contract under test:

- `vague_addressee_message` fires on a synthetic output that carries no
  content keyword from the injected session payload ("on something"
  class) → verdict FAIL.
- A content-bearing output passes → verdict PASS.
- `empty_content_outbound` fires when an empty-content session produces
  an outbound; a refusal (no text) on empty content passes.
- Shape gates: over-cap output fails `length_cap`
  (structural_checks.COORDINATION_ADDRESSEE_LENGTH_CAP, referenced by
  constant); list-shaped output fails `prose_required`.
- The replay (compose) surface returns output + input_payload evidence.

The composer LLM is mocked (no network), same pattern as the other
synthetic-verify tests.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_synthetic_verify_coordination.py -v
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from capabilities.coordination import verify as coordination_verify
from kavi_runtime.structural_checks import COORDINATION_ADDRESSEE_LENGTH_CAP


def _cfg() -> dict[str, Any]:
    return {
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "enable_prompt_caching": False},
    }


def _session_payload(ask: str = "Does Max have cash for Rosa tomorrow morning?") -> dict[str, Any]:
    return {
        "inbound_text": "check with Max if he has cash for Rosa tomorrow",
        "requester_name": "Megha",
        "addressee_name": "Max",
        "coordination_ask": ask,
        "attribution_judgment": {"should_attribute": True, "reason": "personal ask"},
    }


def _mock_composer(monkeypatch: pytest.MonkeyPatch, text: str | None) -> MagicMock:
    """Replace ClaudeClient in the verify module with a double whose
    addressee composer returns `text`."""
    client = MagicMock()
    client.compose_coordination_addressee_message.return_value = {
        "text": text,
        "char_count": len(text or ""),
        "attribution_applied": True,
        "_usage": None,
    }
    monkeypatch.setattr(
        coordination_verify, "ClaudeClient", lambda config: client,
    )
    return client


# ---- selection gate: vague output ------------------------------------------


def test_vague_on_something_output_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The June 3 class: a content-free ping must FAIL the vague gate."""
    _mock_composer(
        monkeypatch,
        "Hey Max, Megha asked me to check with you on something — got a minute?",
    )
    result = coordination_verify.verify_coordination_selection(
        _cfg(), _session_payload(),
    )
    assert result["verdict"] == "FAIL"
    gates = {f["gate"] for f in result["failures"]}
    assert "vague_addressee_message" in gates


def test_content_bearing_output_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_composer(
        monkeypatch,
        "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you "
        "have it on hand, or should one of you withdraw?",
    )
    result = coordination_verify.verify_coordination_selection(
        _cfg(), _session_payload(),
    )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_generic_time_words_do_not_count_as_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'can you handle something tomorrow?' must not pass on 'tomorrow'
    alone — time words and the addressee's own name are not content."""
    _mock_composer(
        monkeypatch,
        "Hey Max, can you handle something tomorrow morning?",
    )
    result = coordination_verify.verify_coordination_selection(
        _cfg(), _session_payload(),
    )
    assert result["verdict"] == "FAIL"
    gates = {f["gate"] for f in result["failures"]}
    assert "vague_addressee_message" in gates


# ---- selection gate: empty-content session ----------------------------------


def test_empty_content_session_with_outbound_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No content → no send. An empty-ask session that still produces an
    outbound fails the empty_content_outbound gate."""
    _mock_composer(monkeypatch, "Hey Max, quick check on a thing!")
    result = coordination_verify.verify_coordination_selection(
        _cfg(), _session_payload(ask=""),
    )
    assert result["verdict"] == "FAIL"
    gates = {f["gate"] for f in result["failures"]}
    assert "empty_content_outbound" in gates


def test_empty_content_session_with_refusal_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composer refusing (no text) on an empty-content session is the
    correct behavior and passes."""
    _mock_composer(monkeypatch, None)
    result = coordination_verify.verify_coordination_selection(
        _cfg(), _session_payload(ask="   "),
    )
    assert result["verdict"] == "PASS"


# ---- shape gates -------------------------------------------------------------


def test_over_cap_output_fails_length_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_text = (
        "Hey Max, Megha mentioned cash for Rosa tomorrow morning. "
        + "Also " * ((COORDINATION_ADDRESSEE_LENGTH_CAP // 5) + 10)
    )
    assert len(long_text) > COORDINATION_ADDRESSEE_LENGTH_CAP
    _mock_composer(monkeypatch, long_text)
    result = coordination_verify.verify_coordination_selection(
        _cfg(), _session_payload(),
    )
    assert result["verdict"] == "FAIL"
    gates = {f["gate"] for f in result["failures"]}
    assert "length_cap" in gates


def test_list_shaped_output_fails_prose_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_composer(
        monkeypatch,
        "Megha needs cash for Rosa:\n- withdraw today\n- or confirm you have it",
    )
    result = coordination_verify.verify_coordination_selection(
        _cfg(), _session_payload(),
    )
    assert result["verdict"] == "FAIL"
    gates = {f["gate"] for f in result["failures"]}
    assert "prose_required" in gates


# ---- replay (compose) surface ------------------------------------------------


def test_replay_returns_output_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_composer(
        monkeypatch,
        "Hey Max, Megha mentioned cash for Rosa tomorrow morning.",
    )
    payload = _session_payload()
    result = coordination_verify.replay_coordination_addressee(_cfg(), payload)
    assert "cash for Rosa" in result["output"]
    assert result["input_payload"]["coordination_ask"] == payload["coordination_ask"]
    assert result["model"] == "claude-sonnet-4-6"


def test_server_endpoints_are_registered() -> None:
    """The runtime endpoint wiring exists for both routes (Phase 0c gate:
    compose AND verify must ship together)."""
    import inspect
    from capabilities.realtime_kavi import server as server_mod
    src = inspect.getsource(server_mod)
    assert '"/synthetic/compose/kavi-coordinates"' in src
    assert '"/synthetic/verify/kavi-coordinates"' in src
    # The endpoint imports from the canonical re-export, which itself pulls
    # from capabilities/coordination/verify.py (deep-verify-parity shape).
    from kavi_runtime import synthetic_compose
    assert synthetic_compose.verify_coordination_selection is (
        coordination_verify.verify_coordination_selection
    )
    assert synthetic_compose.replay_coordination_addressee is (
        coordination_verify.replay_coordination_addressee
    )
