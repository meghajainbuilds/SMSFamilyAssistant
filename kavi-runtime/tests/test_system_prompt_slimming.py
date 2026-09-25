"""2026-09-23 cost pass: each call loads only the prompt blocks it uses.

The email judge was sending the whole inbox-to-task doc (Changelog, Metrics,
Architecture) plus Kavi's voice spec on every email: ~37k tokens to return one
line of JSON, 72% of monthly spend. See docs/cost-story.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.test_email_to_tasks_cache_ttl import _client

DOC = (
    "# inbox-to-task\n\n## TL;DR\n\nsummary-marker\n\n"
    "## Behavior\n\nHARD-RULE-MARKER skip LinkedIn job alerts\n\n### Few-shot examples\n\nEXAMPLE-MARKER\n\n"
    "## Metrics\n\nMETRICS-MARKER\n\n## Changelog\n\nCHANGELOG-MARKER\n"
)


def _prefix(client, **kw) -> str:
    return client._build_system_prompt("email_to_tasks", **kw)[0]["text"]


def _with_persona_and_security(client):
    client._persona = MagicMock(return_value="PERSONA-MARKER")
    client._security_baseline = MagicMock(return_value="SECURITY-MARKER")
    return client


def test_email_judge_loads_behavior_section_only(tmp_path) -> None:
    client = _client(inbox_to_task_text=DOC, tmp_path=tmp_path)
    text = _prefix(client, with_capability_doc=True)
    assert "HARD-RULE-MARKER" in text and "EXAMPLE-MARKER" in text
    for dropped in ("summary-marker", "METRICS-MARKER", "CHANGELOG-MARKER"):
        assert dropped not in text


def test_capability_doc_is_opt_in(tmp_path) -> None:
    client = _client(inbox_to_task_text=DOC, tmp_path=tmp_path)
    assert "HARD-RULE-MARKER" not in _prefix(client)


def test_doc_without_behavior_heading_falls_back_to_full_text(tmp_path) -> None:
    client = _client(inbox_to_task_text="# doc\n\nLEGACY-RULE\n", tmp_path=tmp_path)
    assert "LEGACY-RULE" in _prefix(client, with_capability_doc=True)


def test_json_judge_keeps_security_but_drops_persona(tmp_path) -> None:
    client = _with_persona_and_security(_client(tmp_path=tmp_path))
    text = _prefix(client, with_persona=False)
    assert "SECURITY-MARKER" in text  # untrusted email still gets injection defense
    assert "PERSONA-MARKER" not in text


def test_voiced_composer_default_keeps_persona_and_security(tmp_path) -> None:
    client = _with_persona_and_security(_client(tmp_path=tmp_path))
    text = _prefix(client)
    assert "PERSONA-MARKER" in text and "SECURITY-MARKER" in text


def test_real_email_judge_call_site_uses_slim_prompt() -> None:
    from capabilities.inbox_to_task import compose
    import inspect
    src = inspect.getsource(compose)
    assert "with_persona=False" in src and "with_capability_doc=True" in src
