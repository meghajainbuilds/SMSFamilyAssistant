"""Email judge: structured output, truncation handling, bounded retry.

Regression for the 2026-09-24 investigation: the judge sometimes reasoned
in prose before its JSON, hit the 512-token cap, and the email was logged
as skipped. Worst case (2026-09-23 ChatGPT alert): a complete `skipped`
object, then a truncated `task` object, parsed as `skipped`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from capabilities.inbox_to_task.compose import JUDGMENT_SCHEMA, run_email_to_tasks
from kavi_runtime.claude_client import ClaudeClient

EMAIL = {"id": "m1", "subject": "New app(s) connected to your Microsoft account",
         "from_address": "account-security-noreply@accountprotection.microsoft.com",
         "source_account": "megha@example.com"}

TASK = {"status": "task", "task": {
    "title": "Verify ChatGPT access to Microsoft account", "due": None, "owner": "megha",
    "owner_reason": "unfamiliar OAuth app", "source_email_id": "m1",
    "source_subject": EMAIL["subject"], "confidence": "medium"},
    "reason": "Unfamiliar OAuth app; Q&A skip covers only Kavi's own app.",
    "email_id": None, "subject": None}
SKIP = {"status": "skipped", "task": None, "reason": "LLM judged: marketing",
        "email_id": "m1", "subject": "x"}


def _resp(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)], stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0,
                              cache_creation=None),
    )


def _client(*responses) -> MagicMock:
    c = MagicMock()
    c._build_system_prompt.return_value = [{"type": "text", "text": "sys"}]
    c._per_call_input_token_cap = 100_000
    c._model_for_call_type.return_value = "claude-sonnet-4-6"
    c._max_tokens_for_call_type.return_value = 512
    c._extract_json = ClaudeClient._extract_json
    c._anthropic.messages.create.side_effect = list(responses)
    return c


def test_request_uses_structured_output_schema() -> None:
    c = _client(_resp(json.dumps(TASK)))
    run_email_to_tasks(c, EMAIL)
    kwargs = c._anthropic.messages.create.call_args.kwargs
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA}}


def test_clean_task_returns_documented_shape_without_nulls() -> None:
    out = run_email_to_tasks(_client(_resp(json.dumps(TASK))), EMAIL)
    assert out["status"] == "task" and out["task"]["title"].startswith("Verify ChatGPT")
    assert "email_id" not in out
    assert out["reason"].startswith("Unfamiliar OAuth app")


def test_schema_puts_reason_before_status() -> None:
    # Generation follows schema order; the model must reason before deciding.
    keys = list(JUDGMENT_SCHEMA["properties"])
    assert keys.index("reason") < keys.index("status")


def test_clean_skip_returns_documented_shape_without_task_key() -> None:
    out = run_email_to_tasks(_client(_resp(json.dumps(SKIP))), EMAIL)
    assert out["status"] == "skipped" and "task" not in out
    assert out["reason"] == "LLM judged: marketing"


def test_truncated_reply_is_never_a_decision_even_if_it_parses() -> None:
    # The 2026-09-23 failure: a valid skip object, then a cut-off task.
    flip_flop = json.dumps(SKIP) + ' Wait, re-evaluate. {"status": "task", "task": {"title": "Verify'
    c = _client(_resp(flip_flop, "max_tokens"), _resp(json.dumps(TASK)))
    out = run_email_to_tasks(c, EMAIL)
    assert c._anthropic.messages.create.call_count == 2
    assert out["status"] == "task"
    assert out["_usage"]["attempts"] == 2


def test_retry_uses_tighter_prompt() -> None:
    c = _client(_resp("Looking at this email:", "max_tokens"), _resp(json.dumps(SKIP)))
    run_email_to_tasks(c, EMAIL)
    first, second = (call.kwargs["messages"][0]["content"]
                     for call in c._anthropic.messages.create.call_args_list)
    assert second.startswith(first) and "one short sentence" in second


def test_two_failures_surface_low_confidence_task_not_silent_skip() -> None:
    c = _client(_resp("Looking at", "max_tokens"), _resp("still prose", "max_tokens"))
    out = run_email_to_tasks(c, EMAIL)
    assert c._anthropic.messages.create.call_count == 2  # bounded: never a third call
    assert out["status"] == "task"
    assert out["task"]["confidence"] == "low"
    assert out["task"]["owner"] == "megha"
    assert out["task"]["source_email_id"] == "m1"
    assert len(out["task"]["title"]) <= 80
    assert out["judge_failure"] == "truncated"


def test_failure_on_max_inbox_defaults_owner_to_max() -> None:
    c = _client(_resp("x", "max_tokens"), _resp("y", "max_tokens"))
    out = run_email_to_tasks(c, {**EMAIL, "source_account": "max@example.com"})
    assert out["task"]["owner"] == "max"


def test_usage_sums_both_attempts() -> None:
    c = _client(_resp("x", "max_tokens"), _resp(json.dumps(SKIP)))
    out = run_email_to_tasks(c, EMAIL)
    assert out["_usage"]["input_tokens"] == 200 and out["_usage"]["output_tokens"] == 100
