"""Tests for the persona-loader wiring in `_build_system_prompt` and
`_persona_system_block`.

Spec-IS-the-runtime collapse (2026-05-27): every Kavi-voiced composer
call must carry the persona text from the canonical spec
`capabilities/kavi-persona.md`. These tests assert the assembled system
prompt for representative composer paths includes the loader output.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_build_system_prompt_includes_persona_from_loader.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kavi_runtime import claude_client, persona_loader


# Fixture spec used by every test in this module. Mirrors the section
# shape of the real spec so the loader's H2 boundary detection runs.
_FIXTURE_SPEC = """\
---
name: kavi-persona
status: fixture
---
# Kavi

## TL;DR

Loader-wiring fixture.

## Behavior

### Voice rules

#### Texture

- Good: short, direct sentences.
- Bad: dense convoluted prose.

#### Compliance bundle

- 120 character cap.

### Persona

#### Anticipation

- Good: forward-looking framing.

## System prompt

```xml
<persona>
You are Kavi. Fixture identity.
</persona>
```

## Changelog

- Fixture entry.
"""


@pytest.fixture(autouse=True)
def _reset_loader_cache() -> None:
    persona_loader._reset_cache_for_test()


def _config_for_test(tmp_path: Path) -> dict[str, Any]:
    persona_path = tmp_path / "kavi-persona.md"
    persona_path.write_text(_FIXTURE_SPEC)
    return {
        "claude": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "enable_prompt_caching": False,
        },
        "paths": {
            "skills_dir": str(tmp_path / "skills"),
            "household_md": str(tmp_path / "household.md"),
            "inbox_to_task_md": str(tmp_path / "inbox-to-task.md"),
            "kavi_persona_md": str(persona_path),
        },
    }


@pytest.fixture
def claude_with_stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Same shape as test_persona_refusal_layer.py — stub Anthropic, capture
    system prompts, stub on-disk readers for skill / household / inbox-to-task.
    Persona path is NOT stubbed; the real loader reads the fixture spec
    written into tmp_path by _config_for_test."""
    cfg = _config_for_test(tmp_path)
    captured_systems: list[list[dict[str, Any]]] = []

    class _FakeMessages:
        def create(self, *, model, max_tokens, system, messages):
            captured_systems.append(system)
            class _Block:
                def __init__(self, text: str) -> None:
                    self.text = text
            class _Usage:
                input_tokens = 10
                output_tokens = 10
                cache_creation_input_tokens = 0
                cache_read_input_tokens = 0
            class _Resp:
                content = [_Block('{"message":"stub"}')]
                usage = _Usage()
            return _Resp()

    class _FakeAnthropic:
        def __init__(self, *args, **kwargs) -> None:
            self.messages = _FakeMessages()

    monkeypatch.setattr(claude_client, "Anthropic", _FakeAnthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")

    cli = claude_client.ClaudeClient(cfg)
    monkeypatch.setattr(cli, "_skill", lambda name: f"<<skill:{name}>>")
    monkeypatch.setattr(cli, "_household", lambda: "<<household-md>>")
    monkeypatch.setattr(cli, "_inbox_to_task", lambda: "<<itt>>")

    return cli, captured_systems


def _system_text(captured: list[list[dict[str, Any]]]) -> str:
    if not captured:
        return ""
    return "\n".join(b.get("text", "") for b in captured[-1] if isinstance(b, dict))


# ---- Per-composer assertions ----------------------------------------------


def test_periodic_summary_includes_persona_behavior_block(claude_with_stubs) -> None:
    """The flagship composer per the task spec. At least one per-rule
    heading from Behavior must appear in the assembled system prompt."""
    cli, captured = claude_with_stubs
    cli.compose_periodic_summary(
        queued_tasks=[], pending_questions=[],
        is_rollup=True, time_of_day="9pm",
    )
    text = _system_text(captured)
    # The persona section header lands in the prompt
    assert "## Behavior" in text, (
        "compose_periodic_summary system prompt must include the persona "
        "Behavior section from capabilities/kavi-persona.md"
    )
    # At least one per-rule heading from the rubric
    assert "Voice rules" in text
    # System prompt section's XML identity also lands
    assert "<persona>" in text


def test_conversational_reply_includes_persona(claude_with_stubs) -> None:
    cli, captured = claude_with_stubs
    cli.compose_conversational_reply("hey", recent_outbound=[])
    text = _system_text(captured)
    assert "## Behavior" in text
    assert "<persona>" in text


def test_qa_question_includes_persona(claude_with_stubs) -> None:
    cli, captured = claude_with_stubs
    cli.compose_qa_question(
        title="Bayview tennis registration",
        source_subject="Tennis sign-up open",
        owner_abbrev="MJ",
    )
    text = _system_text(captured)
    assert "## Behavior" in text


def test_post_action_reply_includes_persona(claude_with_stubs) -> None:
    cli, captured = claude_with_stubs
    cli.compose_post_action_reply(
        action_type="mark_done",
        target_title="Test task",
        result="success",
    )
    text = _system_text(captured)
    assert "## Behavior" in text


def test_weekly_self_check_includes_persona(claude_with_stubs) -> None:
    cli, captured = claude_with_stubs
    cli.compose_weekly_self_check()
    text = _system_text(captured)
    assert "## Behavior" in text


def test_coordination_outcome_uses_persona_system_block(claude_with_stubs) -> None:
    """compose_coordination_outcome uses `_persona_system_block` (inline
    system prompt) instead of `_build_system_prompt`. Persona text still
    needs to land — that's the whole point of the spec collapse."""
    cli, captured = claude_with_stubs
    cli.compose_coordination_outcome(
        branch="4b_will_grab",
        commitment_text="Max will grab cash",
        task_id_if_created="t_1",
        task_title_if_created="MM Withdraw cash",
        requester_name="Megha",
        addressee_name="Max",
    )
    text = _system_text(captured)
    assert "## Behavior" in text


def test_coordination_addressee_message_includes_persona(claude_with_stubs) -> None:
    """The addressee-message composer builds its system prompt inline
    with its own assembly path. Persona still must be appended."""
    cli, captured = claude_with_stubs
    cli.compose_coordination_addressee_message(
        inbound_text="ask Max if he has cash",
        requester_name="Megha",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow?",
        attribution_judgment={"should_attribute": True, "reason": "personal ask"},
    )
    text = _system_text(captured)
    assert "## Behavior" in text


# ---- Classifier opt-out ----------------------------------------------------


def test_classifier_calls_skip_persona(claude_with_stubs) -> None:
    """Classifier-only paths opt out of the refusal layer; they should
    also skip persona text since they don't compose voiced output. Keeps
    the cache prefix small for cheap structured-JSON calls."""
    cli, captured = claude_with_stubs
    cli.classify_correction("nope, drop that one")
    text = _system_text(captured)
    assert "## Behavior" not in text, (
        "classifier-only calls should not carry persona text"
    )


# ---- Missing path graceful degradation ------------------------------------


def test_build_system_prompt_works_without_persona_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Back-compat: if kavi_persona_md is not configured (old config
    files predating this change), `_build_system_prompt` must still
    work. Persona text is simply absent from the assembled output."""
    cfg = {
        "claude": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "enable_prompt_caching": False,
        },
        "paths": {
            "skills_dir": str(tmp_path / "skills"),
            "household_md": str(tmp_path / "household.md"),
            "inbox_to_task_md": str(tmp_path / "inbox-to-task.md"),
            # kavi_persona_md intentionally omitted
        },
    }
    captured: list[list[dict[str, Any]]] = []

    class _FakeMessages:
        def create(self, *, model, max_tokens, system, messages):
            captured.append(system)
            class _Block:
                text = '{"message":"stub"}'
            class _Usage:
                input_tokens = 10
                output_tokens = 10
                cache_creation_input_tokens = 0
                cache_read_input_tokens = 0
            class _Resp:
                content = [_Block()]
                usage = _Usage()
            return _Resp()

    class _FakeAnthropic:
        def __init__(self, *args, **kwargs) -> None:
            self.messages = _FakeMessages()

    monkeypatch.setattr(claude_client, "Anthropic", _FakeAnthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    cli = claude_client.ClaudeClient(cfg)
    monkeypatch.setattr(cli, "_skill", lambda name: f"<<skill:{name}>>")
    monkeypatch.setattr(cli, "_household", lambda: "<<household>>")
    monkeypatch.setattr(cli, "_inbox_to_task", lambda: None)

    # Should not raise — back-compat path
    cli.compose_periodic_summary(
        queued_tasks=[], pending_questions=[],
        is_rollup=True, time_of_day="9pm",
    )
    text = "\n".join(b.get("text", "") for b in captured[-1] if isinstance(b, dict))
    assert "## Behavior" not in text, (
        "without kavi_persona_md configured, persona text must be absent"
    )
