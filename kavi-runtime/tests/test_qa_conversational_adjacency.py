"""Conversational-adjacency binding for bare Q&A confirmations.

Tests the runtime rule landed 2026-06-02 PM (replacing the earlier
freshness/120s rule): when Kavi's most recent outbound to Megha was a
`task_notification` (a Q&A question), her bare "keep"/"drop" reply binds
to the matching pending_question — regardless of elapsed time, regardless
of how many other pending Q&As exist in the background. When Kavi's most
recent outbound was something else (periodic_summary, conversational
reply, alert), the rule does NOT fire and control falls through to the
LLM classifier.

The Anita incident (2026-06-01): Megha replied bare "keep" to a Q&A
that had 4 pending. Old behavior was conservative ("no anchor -> empty")
which let the conversational composer fabricate "Kept — task is on the
list" without grounding. This rule's purpose is to bind the reply
correctly so the action layer actually fires.

These tests exercise the data-loading layer the dispatcher relies on
(`_load_recent_outbound`) and the schema fields the dispatcher branches
on (`kind` == "task_notification"). The full dispatch binding is
exercised by the live smoke test after deploy; this regression test
catches schema drift in either the outbound row format or the helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_config(outbound_path: Path) -> dict:
    return {
        "paths": {
            "eval_persona_outbound_judgments_jsonl": str(outbound_path),
        },
    }


@pytest.fixture
def outbound_path(tmp_path: Path) -> Path:
    return tmp_path / "eval-persona-outbound-judgments.jsonl"


def test_last_outbound_task_notification_is_detected(
    outbound_path: Path,
) -> None:
    """Schema gate: a task_notification outbound row is loadable + its
    kind is detectable from the row dict. If this breaks, the dispatcher's
    conversational-adjacency check can no longer fire."""
    rows = [
        {"ts": "2026-06-01T17:33:50Z", "kind": "task_notification",
         "text": "Want me to keep a task to RSVP for Anita's housewarming, or drop it?"},
    ]
    outbound_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    from capabilities.kavi_persona.utils import _load_recent_outbound

    config = _make_config(outbound_path)
    loaded = _load_recent_outbound(config, n=1)

    assert loaded, "outbound rows should load"
    assert loaded[-1].get("kind") == "task_notification"
    assert "Anita" in loaded[-1].get("text", "")


def test_last_outbound_periodic_summary_is_distinguished(
    outbound_path: Path,
) -> None:
    """Schema gate: when Kavi's most recent outbound is a periodic_summary
    (not a task_notification), the conversational-adjacency check must
    NOT fire. This test asserts the helper returns the periodic_summary
    as the most-recent row so the dispatcher can branch correctly."""
    rows = [
        {"ts": "2026-06-02T10:00:01Z", "kind": "task_notification",
         "text": "Want me to keep ...?"},
        {"ts": "2026-06-03T04:00:00Z", "kind": "periodic_summary",
         "text": "9 PM rollup..."},
    ]
    outbound_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    from capabilities.kavi_persona.utils import _load_recent_outbound

    config = _make_config(outbound_path)
    loaded = _load_recent_outbound(config, n=1)

    assert loaded, "outbound rows should load"
    assert loaded[-1].get("kind") == "periodic_summary"
    # The earlier task_notification is NOT what n=1 returns; only the
    # most recent row is loaded. Dispatcher branches on this row alone.
    assert "9 PM" in loaded[-1].get("text", "")


def test_empty_outbound_file_returns_empty_list(
    outbound_path: Path,
) -> None:
    """Failure-safe: a brand-new runtime with no outbound history yet
    must not error. The dispatcher treats an empty list the same as
    'no recent outbound' and falls through to the classifier."""
    outbound_path.write_text("")

    from capabilities.kavi_persona.utils import _load_recent_outbound

    config = _make_config(outbound_path)
    loaded = _load_recent_outbound(config, n=1)

    assert loaded == []


def test_missing_outbound_file_returns_empty_list(
    tmp_path: Path,
) -> None:
    """Failure-safe: missing file path returns empty list, not error."""
    from capabilities.kavi_persona.utils import _load_recent_outbound

    missing = tmp_path / "does-not-exist.jsonl"
    config = _make_config(missing)
    loaded = _load_recent_outbound(config, n=1)

    assert loaded == []
