"""Test that the email-to-tasks system prompt carries `cache_control:
{"type": "ephemeral", "ttl": "1h"}` (REC-2 from audits/token_optimization_
2026-05-06.md).

The 1-hour TTL was chosen because 40% of inbox webhooks land in inter-
arrival gaps > 5 min today, forcing fresh ~16K-token cache writes 4 of
every 10 emails. The 1h tier collapses that re-write churn. This test
guards against an accidental revert to the default 5-min ephemeral TTL.
"""

from __future__ import annotations

import os

from kavi_runtime.claude_client import ClaudeClient


def _client(skill_name_text: str = "stub-skill", household_text: str = "stub-household",
             inbox_to_task_text: str | None = None, tmp_path=None) -> ClaudeClient:
    """Build a ClaudeClient with a stub skills_dir + household_md so
    `_build_system_prompt` reads real files without needing live ones."""
    assert tmp_path is not None
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "email_to_tasks.md").write_text(skill_name_text)
    household = tmp_path / "household.md"
    household.write_text(household_text)
    paths = {
        "skills_dir": str(skills_dir),
        "household_md": str(household),
    }
    if inbox_to_task_text is not None:
        itt = tmp_path / "inbox-to-task.md"
        itt.write_text(inbox_to_task_text)
        paths["inbox_to_task_md"] = str(itt)
    config = {
        "claude": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "enable_prompt_caching": True,
        },
        "paths": paths,
    }
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-used"
    return ClaudeClient(config)


def test_email_to_tasks_system_prompt_uses_1h_cache_ttl(tmp_path) -> None:
    """The email-to-tasks system prefix must carry ttl='1h' (REC-2)."""
    client = _client(tmp_path=tmp_path)
    blocks = client._build_system_prompt("email_to_tasks", cache_ttl="1h")
    assert len(blocks) == 1
    cc = blocks[0].get("cache_control")
    assert cc is not None, "system prefix must carry cache_control"
    assert cc.get("type") == "ephemeral"
    assert cc.get("ttl") == "1h", f"expected ttl='1h' for inbox-to-task, got {cc!r}"


def test_other_callers_keep_5min_default(tmp_path) -> None:
    """Default (no cache_ttl arg) keeps the 5-min ephemeral behavior — ttl
    key absent on the cache_control block. Ensures REC-2 doesn't bleed into
    other call sites."""
    client = _client(tmp_path=tmp_path)
    blocks = client._build_system_prompt("email_to_tasks")
    assert len(blocks) == 1
    cc = blocks[0].get("cache_control")
    assert cc is not None
    assert cc.get("type") == "ephemeral"
    assert "ttl" not in cc, f"default cache_ttl should leave ttl unset (5m), got {cc!r}"
