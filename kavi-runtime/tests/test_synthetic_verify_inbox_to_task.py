"""Tests for the inbox-to-task selection-behavior deep verify endpoint.

Phase 6 of the 2026-06-02 architectural refactor added
`POST /synthetic/verify/inbox-to-task` and the
`verify_email_classify_selection` function backing it. This file covers
the three gates:

- `expected_decision_mismatch` — caller flags an expected decision
  (e.g., "task") and the classifier returned a different one (e.g., "skip").
- `expected_owner_mismatch` — caller flags an expected owner ("MJ" / "MM")
  and the resulting task has a different owner.
- `expected_confidence_mismatch` — caller flags an expected confidence
  ("high" / "medium" / "low") and the result confidence differs.

Each gate test stubs the Anthropic SDK to return a controlled classifier
output, then asserts the verify function returns the right verdict +
failure detail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import synthetic_compose


def _base_config(tmp_path: Path) -> dict[str, Any]:
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
            "eval_persona_outbound_judgments_jsonl":
                str(evals_persona_dir / "eval-persona-outbound-judgments.jsonl"),
            "eval_inbox_judgments_jsonl":
                str(evals_inbox_dir / "eval-inbox-judgments.jsonl"),
            "pending_facts_jsonl": str(state_dir / "pending_facts.jsonl"),
            "kavi_persona_md": str(capabilities_dir / "kavi-persona.md"),
            "inbox_to_task_md": str(capabilities_dir / "inbox-to-task.md"),
            "security_baseline_md": str(capabilities_dir / "security-baseline.md"),
            "skills_dir": str(skills_dir),
            "household_md": str(tmp_path / "household.md"),
            "runs_jsonl": str(state_dir / "runs.jsonl"),
            "runtime_events_jsonl": str(state_dir / "runtime-events.jsonl"),
            "corrections_jsonl": str(state_dir / "corrections.jsonl"),
            "learned_facts_jsonl": str(state_dir / "learned_facts.jsonl"),
            "promoted_patterns_jsonl": str(state_dir / "promoted_patterns.jsonl"),
            "rejected_patterns_jsonl": str(state_dir / "rejected_patterns.jsonl"),
        },
        "guardrails": {"webhook_flood_per_hour": 100},
        "schedule": {
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
        },
    }


def _email_payload() -> dict[str, Any]:
    return {
        "id": "AAMkADtest123",
        "subject": "Decide by Friday",
        "from_name": "Sender",
        "from_address": "sender@example.com",
        "to": ["megha@example.com"],
        "received": "2026-05-29T10:00:00Z",
        "body_text": "We need a decision on the proposal by Friday.",
        "source_account": "megha@example.com",
    }


def _stub_classifier(
    monkeypatch: pytest.MonkeyPatch,
    classifier_output: str,
) -> MagicMock:
    """Make the Anthropic SDK return a fixed classifier response."""
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=classifier_output)]
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


def test_verify_passes_when_expectations_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision=task, owner=megha, confidence=high all match the classifier
    output. Verdict is PASS, no failures."""
    _stub_classifier(
        monkeypatch,
        '{"status": "task", "confidence": "high", '
        '"task": {"title": "Decide on proposal", "owner": "MJ", "body": "By Friday."}, '
        '"reason": "Sender asked Megha to decide; deadline named."}'
    )
    config = _base_config(tmp_path)
    body = {
        "email": _email_payload(),
        "expected_decision": "task",
        "expected_owner": "MJ",
        "expected_confidence": "high",
    }
    result = synthetic_compose.verify_email_classify_selection(config, body)
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_verify_fails_on_decision_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller expected a task but the classifier said skip."""
    _stub_classifier(
        monkeypatch,
        '{"status": "skipped", "confidence": "high", '
        '"reason": "Newsletter, no specific action."}'
    )
    config = _base_config(tmp_path)
    body = {
        "email": _email_payload(),
        "expected_decision": "task",
    }
    result = synthetic_compose.verify_email_classify_selection(config, body)
    assert result["verdict"] == "FAIL"
    gates = [f["gate"] for f in result["failures"]]
    assert "expected_decision_mismatch" in gates


def test_verify_fails_on_owner_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision matched but the owner assignment differs."""
    _stub_classifier(
        monkeypatch,
        '{"status": "task", "confidence": "high", '
        '"task": {"title": "Decide on proposal", "owner": "MM", "body": "By Friday."}, '
        '"reason": "Sender asked Max to decide."}'
    )
    config = _base_config(tmp_path)
    body = {
        "email": _email_payload(),
        "expected_decision": "task",
        "expected_owner": "MJ",
    }
    result = synthetic_compose.verify_email_classify_selection(config, body)
    assert result["verdict"] == "FAIL"
    gates = [f["gate"] for f in result["failures"]]
    assert "expected_owner_mismatch" in gates


def test_verify_fails_on_confidence_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision matched but confidence is medium when high was expected."""
    _stub_classifier(
        monkeypatch,
        '{"status": "task", "confidence": "medium", '
        '"task": {"title": "Decide on proposal", "owner": "MJ", "body": "By Friday."}, '
        '"reason": "Sender asked, somewhat ambiguous."}'
    )
    config = _base_config(tmp_path)
    body = {
        "email": _email_payload(),
        "expected_decision": "task",
        "expected_confidence": "high",
    }
    result = synthetic_compose.verify_email_classify_selection(config, body)
    assert result["verdict"] == "FAIL"
    gates = [f["gate"] for f in result["failures"]]
    assert "expected_confidence_mismatch" in gates


def test_verify_returns_input_payload_for_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify response includes the input it was called with so the
    Verifier sub-agent has the full audit trail."""
    _stub_classifier(
        monkeypatch,
        '{"status": "task", "confidence": "high", '
        '"task": {"title": "T", "owner": "MJ", "body": "B"}, '
        '"reason": "R."}'
    )
    config = _base_config(tmp_path)
    body = {
        "email": _email_payload(),
        "expected_decision": "task",
    }
    result = synthetic_compose.verify_email_classify_selection(config, body)
    assert result["input_payload"]["email"]["subject"] == "Decide by Friday"
    assert result["input_payload"]["expected_decision"] == "task"


def test_verify_no_expectations_always_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no expected_* fields are provided, the verify endpoint runs the
    classifier and returns PASS regardless of the output (caller didn't ask
    for any gate)."""
    _stub_classifier(
        monkeypatch,
        '{"status": "skipped", "confidence": "low", "reason": "anything"}'
    )
    config = _base_config(tmp_path)
    body = {"email": _email_payload()}
    result = synthetic_compose.verify_email_classify_selection(config, body)
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_verify_confidence_read_from_nested_task_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production classifier shape (skills/email_to_tasks.md): confidence
    lives INSIDE the task object, not at the top level. The top-level-only
    read made expected_confidence_mismatch fire with 'got None' on every
    real task decision — caught by the pipeline matrix's maiden staging run
    (2026-06-10, cases clear-create-personal-coordination and
    low-confidence-ambiguous-owner)."""
    _stub_classifier(
        monkeypatch,
        '{"status": "task", '
        '"task": {"title": "Cover family reader slot", "owner": "megha", '
        '"confidence": "high", "source_email_id": "m-1"}, '
        '"reason": "Direct time-bound ask."}'
    )
    config = _base_config(tmp_path)
    body = {
        "email": _email_payload(),
        "expected_decision": "task",
        "expected_owner": "megha",
        "expected_confidence": "high",
    }
    result = synthetic_compose.verify_email_classify_selection(config, body)
    assert result["verdict"] == "PASS", result["failures"]
    assert result["failures"] == []
