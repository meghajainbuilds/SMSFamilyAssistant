"""Parser plumbing for the intent-first dispatch (mocked LLM).

Covers: happy-path parse, the single tighter-prompt retry, the None return
after both attempts, target-id validation (invented ids dropped, empty
target-shaped intents degrade to clarify), and the two deterministic
post-parse rules (cross-owner -> clarify; canonical direction: close beats
qa_keep/qa_drop on the same task).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_reply_intent_parser.py -v
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from capabilities.kavi_persona.reply_intent_parser import (
    CANONICAL_INTENT_TYPES,
    _apply_deterministic_rules,
    _validate_and_normalize,
    parse_reply_intents,
)
from kavi_runtime.claude_client import ClaudeClient


class _FakeClient:
    """Minimal stand-in for ClaudeClient: real _extract_json, fake API."""

    def __init__(self, responses: list[str | Exception]):
        self._responses = list(responses)
        self._caching = False
        self.calls: list[str] = []
        self._anthropic = MagicMock()
        self._anthropic.messages.create.side_effect = self._create

    def _create(self, **kwargs):
        self.calls.append(kwargs["messages"][0]["content"])
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        resp = MagicMock()
        resp.content = [MagicMock(text=nxt)]
        resp.usage = MagicMock(
            input_tokens=100, output_tokens=20,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        return resp

    def _skill(self, name: str) -> str:
        assert name == "reply_intent_parser"
        return "skill body"

    def _model_for_call_type(self, call_type: str) -> str:
        return "claude-sonnet-4-6"

    def _log_call_start(self, *a, **kw) -> float:
        return 0.0

    def _log_call_done(self, *a, **kw) -> None:
        pass

    def _log_call_failed(self, *a, **kw) -> None:
        pass

    _extract_json = staticmethod(ClaudeClient._extract_json)


OPEN_TASKS = [
    {"id": "t-anita", "title": "MJ Decide on Anita Rao House Warming Party invite"},
    {"id": "t-maple", "title": "MJ Complete Maple Street camp forms for Theo"},
    {"id": "t-mm-diapers", "title": "MM Order Ivy diapers refill"},
]
PENDING_QS = [
    {"id": "q-anita", "task_id": "t-anita",
     "task_title_rendered": "MJ Decide on Anita Rao House Warming Party invite"},
]


def _parse(client, **kwargs):
    defaults = dict(
        inbound_text="close the anita task",
        sender="megha",
        open_tasks=OPEN_TASKS,
        pending_questions=PENDING_QS,
    )
    defaults.update(kwargs)
    return parse_reply_intents(client, **defaults)


def test_happy_path_single_call() -> None:
    out = json.dumps({"intents": [{
        "type": "close_task", "target_text": "the anita task",
        "targets": [{"id": "t-anita", "title": "x"}], "confidence": "high",
    }]})
    client = _FakeClient([out])
    intents = _parse(client)
    assert len(client.calls) == 1
    assert intents == [{
        "type": "close_task", "target_text": "the anita task",
        "targets": [{"id": "t-anita",
                     "title": "MJ Decide on Anita Rao House Warming Party invite"}],
        "confidence": "high",
    }]


def test_retry_once_on_garbage_then_succeed() -> None:
    """Cold-fallback policy: exactly ONE retry with a tighter prompt."""
    good = json.dumps({"intents": [{
        "type": "conversational", "target_text": "hey", "targets": [],
        "confidence": "high",
    }]})
    client = _FakeClient(["not json at all", good])
    intents = _parse(client, inbound_text="hey")
    assert len(client.calls) == 2
    assert "did not parse" in client.calls[1]
    assert intents[0]["type"] == "conversational"


def test_both_attempts_fail_returns_none() -> None:
    """Two failures → None (caller ships the safe sentence). Never a
    fabricated intent list, never a third call."""
    client = _FakeClient([RuntimeError("529"), "still garbage"])
    assert _parse(client) is None
    assert len(client.calls) == 2


def test_api_exception_then_success_counts_as_retry() -> None:
    good = json.dumps({"intents": []})
    client = _FakeClient([RuntimeError("overloaded"), good])
    assert _parse(client) == []
    assert len(client.calls) == 2


def test_invented_target_id_dropped_and_degrades_to_clarify() -> None:
    """An invented id is dropped; a task-target intent with no remaining
    valid targets degrades to clarify (never execute-nothing or guess)."""
    out = json.dumps({"intents": [{
        "type": "close_task", "target_text": "the imaginary task",
        "targets": [{"id": "t-invented", "title": "x"}], "confidence": "high",
    }]})
    client = _FakeClient([out])
    intents = _parse(client)
    assert intents[0]["type"] == "clarify"
    assert intents[0]["targets"] == []


def test_unknown_intent_type_skipped() -> None:
    out = json.dumps({"intents": [
        {"type": "explode_task", "target_text": "x", "targets": [], "confidence": "high"},
        {"type": "pause", "target_text": "quiet", "targets": [], "confidence": "high"},
    ]})
    client = _FakeClient([out])
    intents = _parse(client)
    assert [i["type"] for i in intents] == ["pause"]


def test_cross_owner_close_downgrades_to_clarify() -> None:
    """Max closing Megha's MJ task → clarify (single-owner accountability),
    with the matched task kept in targets so the composer can name it."""
    intents = [{
        "type": "close_task", "target_text": "close the anita task",
        "targets": [{"id": "t-anita",
                     "title": "MJ Decide on Anita Rao House Warming Party invite"}],
        "confidence": "high",
    }]
    out = _apply_deterministic_rules(intents, sender="max", pending_questions=[])
    assert out[0]["type"] == "clarify"
    assert out[0]["targets"][0]["id"] == "t-anita"


def test_own_task_close_not_downgraded() -> None:
    intents = [{
        "type": "close_task", "target_text": "diapers done",
        "targets": [{"id": "t-mm-diapers", "title": "MM Order Ivy diapers refill"}],
        "confidence": "high",
    }]
    out = _apply_deterministic_rules(intents, sender="max", pending_questions=[])
    assert out[0]["type"] == "close_task"


def test_canonical_direction_close_beats_qa() -> None:
    """A close_task covering a task with a pending question drops any
    qa_keep/qa_drop on that question (answered-by-close, never coerced)."""
    intents = [
        {"type": "close_task", "target_text": "anita stuff done",
         "targets": [{"id": "t-anita",
                      "title": "MJ Decide on Anita Rao House Warming Party invite"}],
         "confidence": "high"},
        {"type": "qa_keep", "target_text": "anita",
         "targets": [{"id": "q-anita",
                      "title": "MJ Decide on Anita Rao House Warming Party invite"}],
         "confidence": "low"},
    ]
    out = _apply_deterministic_rules(
        intents, sender="megha", pending_questions=PENDING_QS,
    )
    assert [i["type"] for i in out] == ["close_task"]


def test_validate_rejects_non_dict_shapes() -> None:
    assert _validate_and_normalize(None, open_tasks=[], pending_questions=[]) is None
    assert _validate_and_normalize({"nope": []}, open_tasks=[], pending_questions=[]) is None
    assert _validate_and_normalize([], open_tasks=[], pending_questions=[]) is None


def test_vocabulary_matches_contract() -> None:
    """The closed set per ENDPOINT_CONTRACT.md §3."""
    assert CANONICAL_INTENT_TYPES == {
        "qa_keep", "qa_drop", "close_task", "create_task", "delete_task",
        "rename_task", "undo", "pause", "resume", "correction",
        "coordination_reply", "clarify", "conversational",
    }


def test_megha_closing_max_task_executes_normally() -> None:
    """Asymmetric cross-owner rule (frozen-matrix authority, caught on the
    2026-06-11 maiden run): Megha closing an MM task is NOT downgraded —
    she administers the household system. Only the Max→MJ direction
    clarifies (test_cross_owner_close_downgrades_to_clarify above)."""
    intents = [{
        "type": "close_task", "target_text": "bcba paperwork",
        "targets": [{"id": "t-bcba",
                     "title": "MM Submit BCBA paperwork to Hollis"}],
        "confidence": "high",
    }]
    out = _apply_deterministic_rules(intents, sender="megha", pending_questions=[])
    assert out[0]["type"] == "close_task"
    assert out[0]["targets"][0]["id"] == "t-bcba"
