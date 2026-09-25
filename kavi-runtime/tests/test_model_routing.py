"""Tests for ClaudeClient._model_for_call_type routing (item #3 of
2026-05-06 loose-ends pass).

The routing helper resolves a call_type string against config.model_routing
with two fallbacks: model_routing.default, then claude.model. We verify
each of the 7 classifier call types reads its configured route and that
unconfigured call types fall through to default.

These tests do not invoke Anthropic — they exercise the helper logic only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kavi_runtime.claude_client import ClaudeClient


def _client(routing: dict | None = None, model: str = "claude-sonnet-4-6") -> ClaudeClient:
    """Build a ClaudeClient with a stub config. The Anthropic SDK constructor
    requires an api_key; we monkeypatch the resolver so tests don't need a
    real key."""
    config = {
        "claude": {
            "model": model,
            "max_tokens": 1024,
            "enable_prompt_caching": True,
        },
        "paths": {
            "skills_dir": "/tmp/nonexistent",
            "household_md": "/tmp/nonexistent",
        },
    }
    if routing is not None:
        config["model_routing"] = routing
    # The Anthropic class accepts an api_key kwarg; we feed a dummy that
    # never gets used because we don't call .messages.create in these
    # tests.
    import os
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-used"
    return ClaudeClient(config)


@pytest.fixture
def client_with_routing() -> ClaudeClient:
    """Mirror the production config.yaml shape for the 7 routable calls.
    Two are flipped to Haiku; the other 5 sit under default (Sonnet)."""
    return _client(routing={
        "default": "claude-sonnet-4-6",
        "classify_action_intent": "claude-haiku-4-5",
        "classify_qa_reply": "claude-haiku-4-5",
    })


def test_classify_action_intent_routes_to_haiku(client_with_routing: ClaudeClient) -> None:
    assert client_with_routing._model_for_call_type("classify_action_intent") == "claude-haiku-4-5"


def test_classify_qa_reply_routes_to_haiku(client_with_routing: ClaudeClient) -> None:
    assert client_with_routing._model_for_call_type("classify_qa_reply") == "claude-haiku-4-5"


def test_classify_correction_routes_to_default(client_with_routing: ClaudeClient) -> None:
    """Conservative-stay: classify_correction is supported by routing config
    but not flipped today, so default (Sonnet) wins."""
    assert client_with_routing._model_for_call_type("classify_correction") == "claude-sonnet-4-6"


def test_classify_self_check_reply_routes_to_default(client_with_routing: ClaudeClient) -> None:
    assert client_with_routing._model_for_call_type("classify_self_check_reply") == "claude-sonnet-4-6"


def test_classify_pause_intent_routes_to_default(client_with_routing: ClaudeClient) -> None:
    assert client_with_routing._model_for_call_type("classify_pause_intent") == "claude-sonnet-4-6"


def test_classify_coordination_intent_routes_to_default(client_with_routing: ClaudeClient) -> None:
    assert client_with_routing._model_for_call_type("classify_coordination_intent") == "claude-sonnet-4-6"


def test_parse_coordination_reply_routes_to_default(client_with_routing: ClaudeClient) -> None:
    assert client_with_routing._model_for_call_type("parse_coordination_reply") == "claude-sonnet-4-6"


def test_unknown_call_type_falls_through_to_default(client_with_routing: ClaudeClient) -> None:
    """A composer call site (or any call site that hasn't been threaded
    through routing) gets the default model. No regression for existing
    callers."""
    assert client_with_routing._model_for_call_type("compose_periodic_summary") == "claude-sonnet-4-6"
    assert client_with_routing._model_for_call_type(None) == "claude-sonnet-4-6"


def test_routing_falls_through_to_claude_model_when_no_default(tmp_path: Path) -> None:
    """When config has no model_routing block at all, the helper falls back
    to claude.model. This protects existing deploys that haven't added the
    new routing key yet."""
    client = _client(routing=None, model="claude-sonnet-4-6")
    assert client._model_for_call_type("classify_action_intent") == "claude-sonnet-4-6"
