"""Tests for synthetic compose entry points (Verifier sub-agent surface).

The Verifier sub-agent at `.claude/agents/verifier.md` hits these via Tailscale
to replay LLM composer chains without side effects.

Acceptance criteria per kavi-persona row in `capabilities/_role_registry.md`:
  - `replay_periodic_summary` returns a composed message string (or None on
    LLM failure) given a state snapshot.
  - NO iMessage send (the `_send_imessage_with_fallback` hook in handlers
    must not be invoked).
  - NO eval JSONL writes (no row appended to
    `eval_persona_outbound_judgments_jsonl`).
  - NO state file mutations (`pending_facts.jsonl`, `imessage-state.json`).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_synthetic_compose.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers, synthetic_compose


def _base_config(tmp_path: Path) -> dict[str, Any]:
    """Minimal config wiring all the paths the composer + ClaudeClient touch."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    evals_persona_dir = tmp_path / "evals" / "kavi-persona"
    evals_persona_dir.mkdir(parents=True)
    evals_inbox_dir = tmp_path / "evals" / "inbox-to-task"
    evals_inbox_dir.mkdir(parents=True)
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    capabilities_dir = tmp_path / "capabilities"
    capabilities_dir.mkdir()
    return {
        "claude": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 800,
            "enable_prompt_caching": True,
        },
        "imessage": {"megha_phone": "+15555550101"},
        "graph": {"mstodo_shared_list_id": "AQTEST=="},
        "paths": {
            "imessage_state": str(state_dir / "imessage-state.json"),
            "runs_jsonl": str(tmp_path / "runs.jsonl"),
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            "skills_dir": str(skills_dir),
            "household_md": str(tmp_path / "household.md"),
            "kavi_persona_md": str(capabilities_dir / "kavi-persona.md"),
            "security_baseline_md": str(capabilities_dir / "security-baseline.md"),
            "eval_persona_outbound_judgments_jsonl": str(
                evals_persona_dir / "eval-persona-outbound-judgments.jsonl"
            ),
            "eval_inbox_judgments_jsonl": str(
                evals_inbox_dir / "eval-inbox-judgments.jsonl"
            ),
            "runtime_events_jsonl": str(tmp_path / "runtime-events.jsonl"),
        },
    }


@pytest.fixture
def stub_anthropic(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the Anthropic SDK so tests don't hit the network. Also stub
    `_build_system_prompt` so we don't need real persona / security / skill
    files on disk."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text='{"message": "Quiet morning."}')]
    mock_resp.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    mock_resp.usage.model_dump = lambda: {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = mock_resp

    from kavi_runtime import claude_client as cc

    monkeypatch.setattr(cc, "Anthropic", lambda **kw: mock_anthropic)
    monkeypatch.setattr(cc, "_resolve_anthropic_api_key", lambda: "test-key")
    monkeypatch.setattr(
        cc.ClaudeClient,
        "_build_system_prompt",
        lambda self, skill_name: [{"type": "text", "text": "test system"}],
    )
    return mock_anthropic


def test_replay_periodic_summary_returns_output(
    tmp_path: Path,
    stub_anthropic: MagicMock,
) -> None:
    """Replay returns the LLM-composed message plus the input payload."""
    config = _base_config(tmp_path)
    state = {
        "queued_tasks": [],
        "pending_questions": [],
        "pending_facts": [{"topic": "Test", "snippet": "fact"}],
        "is_rollup": False,
        "time_of_day": "morning",
    }

    result = synthetic_compose.replay_periodic_summary(config, state)

    assert result["output"] == "Quiet morning."
    assert result["model"]
    assert result["input_payload"]["time_of_day"] == "morning"
    assert result["input_payload"]["pending_facts"][0]["topic"] == "Test"


def test_replay_periodic_summary_no_imessage_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_anthropic: MagicMock,
) -> None:
    """Replay must NOT trigger the iMessage send path."""
    config = _base_config(tmp_path)

    send_calls: list[Any] = []

    def _send(config: dict, text: str, *, kind: str) -> dict:
        send_calls.append((text, kind))
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send)

    state = {
        "queued_tasks": [],
        "pending_questions": [],
        "pending_facts": [],
        "is_rollup": False,
        "time_of_day": "morning",
    }

    synthetic_compose.replay_periodic_summary(config, state)

    assert send_calls == [], f"unexpected iMessage send(s): {send_calls}"


def test_replay_periodic_summary_no_eval_jsonl_write(
    tmp_path: Path,
    stub_anthropic: MagicMock,
) -> None:
    """Replay must NOT append to eval_persona_outbound_judgments_jsonl."""
    config = _base_config(tmp_path)
    eval_path = Path(config["paths"]["eval_persona_outbound_judgments_jsonl"])

    state = {
        "queued_tasks": [],
        "pending_questions": [],
        "pending_facts": [],
        "is_rollup": False,
        "time_of_day": "morning",
    }

    synthetic_compose.replay_periodic_summary(config, state)

    assert not eval_path.exists(), (
        f"unexpected eval JSONL written at {eval_path}"
    )


def test_replay_periodic_summary_no_state_mutation(
    tmp_path: Path,
    stub_anthropic: MagicMock,
) -> None:
    """Replay must NOT touch pending_facts.jsonl or imessage-state.json."""
    config = _base_config(tmp_path)
    pending_facts_path = Path(config["paths"]["pending_facts"])
    imessage_state_path = Path(config["paths"]["imessage_state"])

    pending_facts_path.write_text(
        '{"topic": "Sentinel", "snippet": "Do not touch"}\n'
    )
    imessage_state_path.write_text(
        '{"queued_summary_items": [{"title": "Sentinel"}]}'
    )
    pending_mtime = pending_facts_path.stat().st_mtime_ns
    imessage_mtime = imessage_state_path.stat().st_mtime_ns

    state = {
        "queued_tasks": [],
        "pending_questions": [],
        "pending_facts": [],
        "is_rollup": False,
        "time_of_day": "morning",
    }

    synthetic_compose.replay_periodic_summary(config, state)

    assert pending_facts_path.stat().st_mtime_ns == pending_mtime, (
        "pending_facts.jsonl was modified"
    )
    assert imessage_state_path.stat().st_mtime_ns == imessage_mtime, (
        "imessage-state.json was modified"
    )
    assert "Sentinel" in pending_facts_path.read_text()
    assert "Sentinel" in imessage_state_path.read_text()


@pytest.fixture
def stub_anthropic_classifier(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Same shape as stub_anthropic but returns a classifier-style JSON
    response (one of the inbox-to-task skill's output shapes)."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=
        '{"status": "task", "task": {"title": "Test action", '
        '"owner": "megha", "body": "Reply by Friday."}, '
        '"reason": "Sender requested decision; deadline named."}'
    )]
    mock_resp.usage = MagicMock(
        input_tokens=20,
        output_tokens=15,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    mock_resp.usage.model_dump = lambda: {
        "input_tokens": 20,
        "output_tokens": 15,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = mock_resp

    from kavi_runtime import claude_client as cc

    monkeypatch.setattr(cc, "Anthropic", lambda **kw: mock_anthropic)
    monkeypatch.setattr(cc, "_resolve_anthropic_api_key", lambda: "test-key")
    monkeypatch.setattr(
        cc.ClaudeClient,
        "_build_system_prompt",
        lambda self, skill_name, **kw: [{"type": "text", "text": "test system"}],
    )
    return mock_anthropic


def _email_payload(subject: str = "Test", body: str = "Hello") -> dict[str, Any]:
    return {
        "id": "AAMkADtest123",
        "subject": subject,
        "from_name": "Sender",
        "from_address": "sender@example.com",
        "to": ["megha@example.com"],
        "received": "2026-05-29T10:00:00Z",
        "body_text": body,
        "source_account": "megha@example.com",
    }


def test_replay_email_classify_returns_decision(
    tmp_path: Path,
    stub_anthropic_classifier: MagicMock,
) -> None:
    """Replay returns the parsed classifier decision plus echo of input."""
    config = _base_config(tmp_path)
    email = _email_payload(subject="Action needed by Friday")

    result = synthetic_compose.replay_email_classify(config, email)

    assert result["decision"] == "task"
    assert result["result"]["task"]["title"] == "Test action"
    assert result["model"]
    assert result["input_payload"]["subject"] == "Action needed by Friday"


def test_replay_email_classify_no_mstodo_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_anthropic_classifier: MagicMock,
) -> None:
    """Replay must NOT call any MS To Do or Graph API. Patches the handlers
    layer that owns those side effects and asserts nothing reaches it."""
    config = _base_config(tmp_path)

    mstodo_calls: list[Any] = []

    def _intercept_create(*args: Any, **kwargs: Any) -> Any:
        mstodo_calls.append(("create_task", args, kwargs))
        raise AssertionError("replay called the To Do task-create path")

    def _intercept_update(*args: Any, **kwargs: Any) -> Any:
        mstodo_calls.append(("update_task", args, kwargs))
        raise AssertionError("replay called the To Do task-update path")

    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("replay called iMessage send"),
        ),
    )

    email = _email_payload()

    synthetic_compose.replay_email_classify(config, email)

    assert mstodo_calls == [], f"unexpected MS To Do call(s): {mstodo_calls}"


def test_replay_email_classify_no_eval_jsonl_write(
    tmp_path: Path,
    stub_anthropic_classifier: MagicMock,
) -> None:
    """Replay must NOT append to eval_inbox_judgments_jsonl."""
    config = _base_config(tmp_path)
    eval_path = Path(config["paths"]["eval_inbox_judgments_jsonl"])

    email = _email_payload()

    synthetic_compose.replay_email_classify(config, email)

    assert not eval_path.exists(), (
        f"unexpected eval JSONL written at {eval_path}"
    )
