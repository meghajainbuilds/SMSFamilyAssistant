"""Selection must pass FULL pending-fact text to composers (2026-06-10).

June 3 incident, digest half: the coordination intent fact begins with
~45 chars of audit preamble ("Megha asked Kavi to coordinate with Max
on: "). Selection truncated fact text to a 60-char snippet, so the digest
composer received almost no actual content and shipped "coordinating
with Max on something" for a week.

Contract under test:

- `_read_pending_facts_for_summary` strips the coordination preamble and
  passes the FULL remaining fact text (no truncation). Selection must
  not pre-compress; the composer honors the outbound length cap itself.
- `compose_periodic_summary` marshals the full snippet into the LLM
  input payload (the old composer-side re-cap is gone).
- `compose_conversational_reply` accepts pending_facts and marshals the
  full snippet into its LLM input payload (the "what is the task?"
  answerability half).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_pending_facts_full_text_selection.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from capabilities.kavi_persona.selection import _read_pending_facts_for_summary
from capabilities.kavi_persona.composers.periodic_summary import (
    compose_periodic_summary,
)
from capabilities.kavi_persona.composers.conversational import (
    compose_conversational_reply,
)


BOILERPLATE = "Megha asked Kavi to coordinate with Max on: "
ASK = (
    "Can you meet the teachers tomorrow at 3:00 or 3:35 for the "
    "parent-teacher conference about the reading group placement?"
)
FACT_TEXT = BOILERPLATE + ASK


@pytest.fixture
def cfg_with_coordination_fact(tmp_path: Path) -> dict[str, Any]:
    pending_path = tmp_path / "pending_facts.jsonl"
    rows = [
        {
            "pending_id": "p1",
            "ts": "2026-06-03T18:00:00Z",
            "fact_text": FACT_TEXT,
            "scope": "household",
            "source_decision_id": "s_2026-06-03_session",
            "expires_at": None,
            "status": "pending_confirmation",
            "inbound_source": "imessage coord-s_2026-06-03_session",
            "todo": "TODO: Megha to confirm",
        },
    ]
    pending_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return {
        "paths": {
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(pending_path),
        },
    }


# ---- selection layer --------------------------------------------------------


def test_selection_passes_full_fact_text_no_truncation(
    cfg_with_coordination_fact: dict[str, Any],
) -> None:
    rows = _read_pending_facts_for_summary(cfg_with_coordination_fact, top_n=3)
    assert len(rows) == 1
    snippet = rows[0]["snippet"]
    # No truncation: the full ask survives (it is well over the old 60-char
    # cap), including the content at the tail.
    assert snippet == ASK
    assert len(snippet) > 60, "old 60-char cap would have clipped this"
    assert "reading group placement" in snippet, (
        "tail content must survive — the old cap dropped everything after "
        "char 60"
    )


def test_selection_strips_coordination_boilerplate(
    cfg_with_coordination_fact: dict[str, Any],
) -> None:
    rows = _read_pending_facts_for_summary(cfg_with_coordination_fact, top_n=3)
    snippet = rows[0]["snippet"]
    assert "asked Kavi to coordinate" not in snippet, (
        "audit preamble is bookkeeping, not content — it must be stripped "
        "before the LLM sees the fact"
    )
    assert snippet.startswith("Can you meet the teachers")


def test_selection_keeps_topic_field_shape(
    cfg_with_coordination_fact: dict[str, Any],
) -> None:
    """Topic stays as-is per the 2026-06-10 brief (only snippet changed)."""
    rows = _read_pending_facts_for_summary(cfg_with_coordination_fact, top_n=3)
    assert rows[0]["topic"]
    assert len(rows[0]["topic"]) <= 30


def test_selection_leaves_non_coordination_facts_untouched(
    tmp_path: Path,
) -> None:
    """Facts that don't carry the coordination preamble pass through whole."""
    pending_path = tmp_path / "pending_facts.jsonl"
    text = "Nadia asked us to soak black beans Monday night before she arrives for the prep"
    pending_path.write_text(json.dumps({
        "pending_id": "p9", "ts": "2026-06-03T18:00:00Z",
        "fact_text": text, "scope": "household",
        "source_decision_id": None, "expires_at": None,
        "status": "pending_confirmation", "inbound_source": "imessage from Nadia",
        "todo": "",
    }) + "\n")
    cfg = {"paths": {
        "learned_facts": str(tmp_path / "learned_facts.jsonl"),
        "pending_facts": str(pending_path),
    }}
    rows = _read_pending_facts_for_summary(cfg, top_n=3)
    assert rows[0]["snippet"] == text


# ---- composer marshaling ----------------------------------------------------


def _fake_compose_client() -> MagicMock:
    """A client double for the composer functions (they take `client` as the
    first arg). The Anthropic call is captured, not executed."""
    client = MagicMock()
    client._build_system_prompt.return_value = []
    client._model_for_call_type.return_value = "claude-sonnet-4-6"
    client._log_call_start.return_value = 0.0
    resp = MagicMock()
    resp.content = [MagicMock(text='{"message": "ok"}')]
    resp.usage = MagicMock()
    resp.usage.model_dump.return_value = {"input_tokens": 1, "output_tokens": 1}
    client._anthropic.messages.create.return_value = resp
    client._extract_json.return_value = {"message": "ok"}
    return client


def _captured_user_msg(client: MagicMock) -> str:
    call = client._anthropic.messages.create.call_args
    return call.kwargs["messages"][0]["content"]


def test_periodic_summary_composer_input_carries_full_snippet() -> None:
    client = _fake_compose_client()
    compose_periodic_summary(
        client,
        queued_tasks=[], pending_questions=[],
        is_rollup=True, time_of_day="9pm",
        pending_facts=[{"topic": "Max", "snippet": ASK}],
    )
    user_msg = _captured_user_msg(client)
    assert ASK in user_msg, (
        "the composer must receive the FULL fact text — re-capping at the "
        "marshaling layer reintroduces the starved-digest bug"
    )


def test_conversational_composer_input_carries_pending_facts() -> None:
    client = _fake_compose_client()
    compose_conversational_reply(
        client,
        "what is the task?",
        recent_outbound=[{"ts": "t", "kind": "periodic_summary", "text": "digest"}],
        pending_facts=[{"topic": "Max", "snippet": ASK}],
    )
    user_msg = _captured_user_msg(client)
    assert ASK in user_msg
    assert "pending_facts" in user_msg


def test_conversational_composer_defaults_to_empty_facts() -> None:
    """Back-compat: callers that don't pass pending_facts get an empty list
    in the payload, not an error."""
    client = _fake_compose_client()
    compose_conversational_reply(client, "hey", recent_outbound=[])
    user_msg = _captured_user_msg(client)
    payload = json.loads(user_msg.split("<input>\n", 1)[1].split("\n</input>")[0])
    assert payload["pending_facts"] == []
