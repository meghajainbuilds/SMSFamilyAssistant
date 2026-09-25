"""Tests for ClaudeClient._max_tokens_for_call_type routing (REC-4 from
audits/token_optimization_2026-05-06.md).

Tightening compose_email_to_tasks_judgment from 1024 to 512 doesn't save
$/mo directly (output billed per token actually emitted), but it caps
blast radius on jailbreak attempts: a runaway 1024-token output costs
~$0.015 vs 512 capped at ~$0.0075. P95 output is 284 tokens; 512 is
1.8x P95 with no real-call truncation risk.
"""

from __future__ import annotations

import os

from kavi_runtime.claude_client import ClaudeClient


def _client(routing: dict | None = None, default_max_tokens: int = 1024) -> ClaudeClient:
    config = {
        "claude": {
            "model": "claude-sonnet-4-6",
            "max_tokens": default_max_tokens,
            "enable_prompt_caching": True,
        },
        "paths": {
            "skills_dir": "/tmp/nonexistent",
            "household_md": "/tmp/nonexistent",
        },
    }
    if routing is not None:
        config["max_tokens_routing"] = routing
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-used"
    return ClaudeClient(config)


def test_email_to_tasks_routes_to_512() -> None:
    """compose_email_to_tasks_judgment is capped at 512 per REC-4."""
    client = _client(routing={"compose_email_to_tasks_judgment": 512})
    assert client._max_tokens_for_call_type("compose_email_to_tasks_judgment") == 512


def test_unrouted_call_falls_through_to_default() -> None:
    """A call type not in the routing map falls through to claude.max_tokens."""
    client = _client(routing={"compose_email_to_tasks_judgment": 512})
    assert client._max_tokens_for_call_type("compose_post_action_reply") == 1024


def test_default_key_in_routing_overrides_legacy() -> None:
    """A `default` key in max_tokens_routing wins over claude.max_tokens
    for any call type that isn't explicitly listed."""
    client = _client(
        routing={"default": 600, "compose_email_to_tasks_judgment": 512},
        default_max_tokens=1024,
    )
    assert client._max_tokens_for_call_type("compose_email_to_tasks_judgment") == 512
    assert client._max_tokens_for_call_type("classify_correction") == 600


def test_no_routing_config_falls_back_to_legacy() -> None:
    """When max_tokens_routing is absent entirely, all calls use the legacy
    claude.max_tokens — no regression on installs that haven't added the map."""
    client = _client(routing=None, default_max_tokens=1024)
    assert client._max_tokens_for_call_type("compose_email_to_tasks_judgment") == 1024
    assert client._max_tokens_for_call_type(None) == 1024
