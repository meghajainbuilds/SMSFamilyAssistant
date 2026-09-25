"""Tests for Fix 2 (2026-05-08): the action clarifying composer must ask,
never claim.

The 2026-05-07 Oak Circle Tea production trace surfaced two cases where the
clarifying composer (which has NOT executed any tool) emitted past-tense
state claims like "All the Oak Circle Tea tasks are already marked done" or
"Both Oak Circle Tea tasks are already marked completed in the system." Each
was a lie — no PATCH had run.

Defense:
1. Skill prompt (`skills/action_clarifying_reply_composer.md`) gets a
   top-level rule banning state claims and a list of forbidden phrases.
2. Deterministic post-composer check in `claude_client.compose_action_clarifying_reply`
   matches the LLM output against `FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES`
   and rewrites to a safe fallback question if any phrase is present.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_action_clarifying_composer_fix2.py -v
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime.claude_client import (
    ClaudeClient,
    _safe_clarify_fallback_question,
)
from kavi_runtime.structural_checks import (
    FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES,
    text_contains_forbidden_clarify_state_claim,
)


# ---- Forbidden-phrase matcher -------------------------------------------


def test_forbidden_phrase_matcher_catches_each_listed_phrase() -> None:
    """Every phrase in FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES must trigger
    the matcher. Uses the production-trace text shape for each."""
    samples = {
        "already done": "All the tasks are already done.",
        "already marked": "Both UW tasks are already marked done.",
        "already completed": "The Oak Circle Tea items are already completed in the system.",
        "marked done": "Everything's marked done on my end.",
        "done in the system": "It's done in the system.",
        "completed in the system": "All four are completed in the system.",
        "nothing left open": "Nothing left open for Oak Circle Tea.",
        "nothing left on my end": "Nothing left on my end — all good.",
    }
    for phrase, sample in samples.items():
        assert phrase in [p for p in FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES]
        assert text_contains_forbidden_clarify_state_claim(sample) is True, (
            f"matcher missed sample for phrase {phrase!r}: {sample!r}"
        )


def test_forbidden_phrase_matcher_case_insensitive() -> None:
    """Matcher is case-insensitive — the LLM may capitalize differently."""
    assert text_contains_forbidden_clarify_state_claim(
        "ALL THE TASKS ARE ALREADY DONE."
    ) is True
    assert text_contains_forbidden_clarify_state_claim(
        "All The Tasks Are Marked Done."
    ) is True


def test_forbidden_phrase_matcher_passes_legitimate_questions() -> None:
    """Legitimate clarifying questions and acks must not trip the matcher.
    The phrases are distinctive multi-word state claims; questions and
    pre-action prompts use different language."""
    legitimate = [
        "Found 4 Oak Circle Tea tasks. Mark all four?",
        "Want me to mark it done?",
        "Confirm to mark done?",
        "Two UW tasks open: $630 bill and the appointment. Mark both?",
        "I haven't run anything yet — can you name the task?",
        "Got Northgate Medical balance ($630). Confirm to mark done?",
        # Even past-tense success acks pass the forbidden-phrase matcher;
        # they're caught by G-A1 if context has no actions_executed.
        "Marked all four done.",
        "Closed out the UW task.",
    ]
    for sample in legitimate:
        assert text_contains_forbidden_clarify_state_claim(sample) is False, (
            f"matcher false-positive on: {sample!r}"
        )


def test_forbidden_phrase_matcher_handles_none_and_empty() -> None:
    """Defensive: matcher handles empty / None inputs without raising."""
    assert text_contains_forbidden_clarify_state_claim("") is False
    assert text_contains_forbidden_clarify_state_claim(None) is False  # type: ignore[arg-type]


# ---- Safe fallback question generator -----------------------------------


def test_safe_fallback_for_mark_done_with_target() -> None:
    """Fallback names the user's quoted target so she can correct it."""
    out = _safe_clarify_fallback_question(
        free_text="mark oak circle tea done",
        action_type="mark_done",
        target_text="Oak Circle Tea",
        open_tasks=[
            {"id": "t1", "title": "MJ Confirm guests Oak Circle Tea"},
        ],
    )
    assert "haven't run" in out.lower() or "haven't" in out.lower()
    assert "Oak Circle" in out  # quoted target
    assert len(out) <= 240


def test_safe_fallback_for_mark_done_no_target() -> None:
    """Fallback for empty target_text asks for an exact task name."""
    out = _safe_clarify_fallback_question(
        free_text="mark them done",
        action_type="mark_done",
        target_text="",
        open_tasks=[{"id": "t1", "title": "MJ X"}],
    )
    assert "haven't" in out.lower()
    assert "task" in out.lower()


def test_safe_fallback_for_update_cancel() -> None:
    """update / cancel verbs get a generic ask-which-task fallback."""
    for action in ("update", "cancel"):
        out = _safe_clarify_fallback_question(
            free_text=f"{action} the boonli thing",
            action_type=action,
            target_text="the boonli thing",
            open_tasks=[{"id": "t1", "title": "MJ Boonli"}],
        )
        assert "haven't" in out.lower()


def test_safe_fallback_no_open_tasks() -> None:
    """Fallback handles the no-open-tasks edge cleanly."""
    out = _safe_clarify_fallback_question(
        free_text="mark oak circle tea done",
        action_type="mark_done",
        target_text="Oak Circle Tea",
        open_tasks=[],
    )
    assert "haven't" in out.lower()


# ---- compose_action_clarifying_reply post-check integration ------------


def _build_claude_client_with_mocked_anthropic(
    monkeypatch: pytest.MonkeyPatch, mock_response_text: str,
) -> ClaudeClient:
    """Build a real ClaudeClient with a mocked Anthropic API response.

    The post-composer check lives in the real method; we only need to mock
    the API call itself so the LLM "returns" the test-shaped output.
    """
    from pathlib import Path as _Path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")

    config = {
        "paths": {
            "skills_dir": str(
                _Path(__file__).resolve().parent.parent / "skills"
            ),
            "household_md": "/tmp/nonexistent_household.md",
        },
        "claude": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 400,
            "enable_prompt_caching": False,
            "max_retries": 1,
        },
    }
    client = ClaudeClient(config)

    # Replace the Anthropic client's `messages.create` with a mock that
    # returns the test response shape.
    mock_resp = MagicMock()
    mock_block = MagicMock()
    mock_block.text = mock_response_text
    mock_resp.content = [mock_block]
    mock_resp.usage.model_dump.return_value = {
        "input_tokens": 100, "output_tokens": 50,
    }

    mock_messages = MagicMock()
    mock_messages.create.return_value = mock_resp

    mock_anthropic = MagicMock()
    mock_anthropic.messages = mock_messages
    client._anthropic = mock_anthropic

    return client


def test_compose_clarifying_rewrites_state_claim_to_safe_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM emits the 2026-05-07 production-trace lie. The deterministic
    post-check rewrites to a safe fallback question. Megha never reads the
    forbidden state-claim text."""
    hallucinated = (
        '{"message": "All the Oak Circle Tea tasks are already marked done — '
        'nothing left open on my end."}'
    )
    client = _build_claude_client_with_mocked_anthropic(
        monkeypatch, hallucinated,
    )

    out = client.compose_action_clarifying_reply(
        free_text="Mark all oak circle tea items done",
        action_type="mark_done",
        target_text="",
        open_tasks=[
            {"id": "t1", "title": "MJ Confirm guests Oak Circle Tea", "status": "notStarted"},
            {"id": "t2", "title": "MJ Forward Zoom link Oak Circle Tea", "status": "notStarted"},
            {"id": "t3", "title": "MJ Theo's clothes Oak Circle Tea", "status": "notStarted"},
            {"id": "t4", "title": "MJ Gluten-free baked goods Oak Circle Tea", "status": "notStarted"},
        ],
    )

    assert out is not None
    # The forbidden phrase is gone.
    assert "already marked done" not in out.lower()
    assert "nothing left open" not in out.lower()
    # The fallback declares un-executed state.
    assert "haven't" in out.lower()


def test_compose_clarifying_passes_normal_question_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM emits a clean clarifying question with no forbidden phrases.
    Output ships unchanged — no rewrite."""
    clean = (
        '{"message": "Found 4 Oak Circle Tea tasks: confirm guests, forward Zoom '
        "link, Theo's clothes, gluten-free baked goods. Mark all four?\"}"
    )
    client = _build_claude_client_with_mocked_anthropic(monkeypatch, clean)

    out = client.compose_action_clarifying_reply(
        free_text="Mark all oak circle tea items done",
        action_type="mark_done",
        target_text="",
        open_tasks=[
            {"id": "t1", "title": "MJ Confirm guests Oak Circle Tea"},
        ],
    )
    assert out is not None
    assert "Found 4 Oak Circle Tea tasks" in out
    assert "Mark all four?" in out


def test_compose_clarifying_rewrites_each_individual_forbidden_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every phrase in FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES individually
    triggers the rewrite, so a future LLM that drops one phrase but keeps
    another still gets caught."""
    for phrase in FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES:
        hallucinated = f'{{"message": "Tests show {phrase} — confirm?"}}'
        client = _build_claude_client_with_mocked_anthropic(
            monkeypatch, hallucinated,
        )
        out = client.compose_action_clarifying_reply(
            free_text="mark them done",
            action_type="mark_done",
            target_text="",
            open_tasks=[{"id": "t1", "title": "MJ X"}],
        )
        assert out is not None
        assert phrase not in out.lower(), (
            f"phrase {phrase!r} survived the post-check rewrite: {out!r}"
        )
