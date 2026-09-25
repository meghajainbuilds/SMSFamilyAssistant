"""Live-LLM snapshot test fixtures.

Builds a real ClaudeClient pointed at the source skills/ + household.md
+ capabilities/inbox-to-task.md. Marks every test in this directory
with `live_llm` so `pytest -m live_llm` runs them as a unit and the
default test run skips them.

The `claude_live` fixture is module-scoped so we instantiate one
client per test file (one set of skill prompts / cache prefix per
file) but parameterised tests within a file share the client and its
prompt cache.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from kavi_runtime.claude_client import ClaudeClient


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = Path(__file__).resolve().parents[2]


def _has_anthropic_key() -> bool:
    """True if either ANTHROPIC_API_KEY env var is set or the macOS
    Keychain has the key. We check env first because it's cheap; the
    Keychain path is wrapped in a broad try/except inside the
    secrets.read_secret resolver."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        from kavi_runtime import secrets as kavi_secrets
        return bool(kavi_secrets.read_secret("anthropic_api_key"))
    except Exception:
        return False


def _live_config() -> dict[str, Any]:
    """Live ClaudeClient config: real skills, real household, real
    capability. Mirrors prod config.yaml's claude.* + paths.* keys."""
    return {
        "claude": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "enable_prompt_caching": True,
        },
        "paths": {
            "skills_dir": str(RUNTIME_ROOT / "skills"),
            "household_md": str(REPO_ROOT / "household.md"),
            "inbox_to_task_md": str(REPO_ROOT / "capabilities" / "inbox-to-task.md"),
        },
        "model_routing": {
            "default": "claude-sonnet-4-6",
            "classify_action_intent": "claude-haiku-4-5",
            "classify_qa_reply": "claude-haiku-4-5",
        },
        "max_tokens_routing": {},
        "guardrails": {"per_call_input_token_cap": 50000},
    }


@pytest.fixture(scope="module")
def claude_live() -> ClaudeClient:
    """Module-scoped real ClaudeClient. Skips the test if no API key
    is available — keeps default `pytest` runs safe even if a future
    contributor forgets the marker filter."""
    if not _has_anthropic_key():
        pytest.skip("ANTHROPIC_API_KEY not set; live_llm tests need real auth")
    return ClaudeClient(_live_config())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-tag every test in this directory with the `live_llm`
    marker AND auto-skip them unless the user explicitly opted in via
    `-m live_llm` (or set RUN_LIVE_LLM=1). Default `pytest` runs MUST
    NOT hit the real Anthropic API; that's the contract."""
    snapshots_dir = str(Path(__file__).parent.resolve())
    marker_expr = (config.getoption("-m") or "").strip()
    user_opted_in = (
        "live_llm" in marker_expr or os.environ.get("RUN_LIVE_LLM") == "1"
    )
    skip_marker = pytest.mark.skip(
        reason="live_llm tests skipped by default; use `pytest -m live_llm`",
    )
    for item in items:
        if snapshots_dir in str(Path(item.fspath).resolve()):
            item.add_marker(pytest.mark.live_llm)
            if not user_opted_in:
                item.add_marker(skip_marker)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_llm: tests that hit the real Anthropic API; "
        "skipped by default; run via `pytest -m live_llm`",
    )
