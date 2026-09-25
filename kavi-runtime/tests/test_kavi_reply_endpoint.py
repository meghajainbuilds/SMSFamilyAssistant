"""The /synthetic/{compose,verify}/kavi-reply endpoints + replay wiring.

Covers: both routes registered, contract schema enforcement at the HTTP
layer (unknown field → 400, missing expectations → 400), compose-mode
tolerance of expectation fields, and the replay function running the REAL
parser/composer code paths against a stubbed ClaudeClient with the
dry-run executors making zero Graph calls (graph is None inside replay —
any touch would raise).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_kavi_reply_endpoint.py -v
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from capabilities.realtime_kavi.server import build_app
from kavi_runtime import synthetic_compose


def _server_config(tmp_path) -> dict[str, Any]:
    return {
        "bluebubbles": {"inbound_webhook_path": "/imessage"},
        "imessage": {"megha_phone": "+15555550101"},
        "graph": {"mstodo_shared_list_id": "LIST"},
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "enable_prompt_caching": False},
        "model_routing": {},
        "paths": {
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "out.jsonl"),
        },
    }


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "inbound_text": "Close it",
        "sender": "megha",
        "recent_outbound": [{"kind": "periodic_summary",
                             "text": "Looks like you already paid Boonli, want to close it?"}],
        "recent_inbound": [],
        "pending_questions": [],
        "pending_facts": [],
        "open_tasks": [{"id": "t-boonli", "title": "MJ Pay Boonli invoice for June lunches"}],
        "expected_intents": [{"type": "close_task", "target_keyword": "Boonli"}],
        "forbidden_intents": [{"type": "clarify", "target_keyword": None}],
    }
    base.update(overrides)
    return base


class _StubClaudeClient:
    """Replaces ClaudeClient inside replay_kavi_reply. Pinned parser output
    + composed reply; no SDK, no keychain."""

    _intents_json: list[dict[str, Any]] | None = None
    _reply: str | None = None

    def __init__(self, config: dict) -> None:
        self._config = config

    def parse_reply_intents(self, **kwargs):
        return type(self)._intents_json

    def compose_kavi_reply(self, **kwargs):
        return type(self)._reply


def _stub_llm(intents, reply):
    _StubClaudeClient._intents_json = intents
    _StubClaudeClient._reply = reply
    return patch("kavi_runtime.claude_client.ClaudeClient", new=_StubClaudeClient)


# ---- routes registered -------------------------------------------------------


def test_both_routes_registered(tmp_path) -> None:
    app = build_app(_server_config(tmp_path))
    paths = {r.path for r in app.routes}
    assert "/synthetic/compose/kavi-reply" in paths
    assert "/synthetic/verify/kavi-reply" in paths


# ---- schema enforcement at the HTTP layer -------------------------------------


def test_verify_unknown_field_rejected_400(tmp_path) -> None:
    client = TestClient(build_app(_server_config(tmp_path)))
    body = _body()
    body["expected_intent"] = []  # typo drift — contract §1 demands 400
    resp = client.post("/synthetic/verify/kavi-reply", json=body)
    assert resp.status_code == 400
    assert "expected_intent" in resp.json()["detail"]


def test_verify_missing_expectations_rejected_400(tmp_path) -> None:
    client = TestClient(build_app(_server_config(tmp_path)))
    body = _body()
    del body["expected_intents"]
    resp = client.post("/synthetic/verify/kavi-reply", json=body)
    assert resp.status_code == 400


def test_verify_bad_sender_rejected_400(tmp_path) -> None:
    client = TestClient(build_app(_server_config(tmp_path)))
    resp = client.post("/synthetic/verify/kavi-reply", json=_body(sender="theo"))
    assert resp.status_code == 400


def test_compose_tolerates_expectation_fields(tmp_path) -> None:
    """Investigators replay matrix rows verbatim against the compose route;
    the two verifier-only fields are ignored, not rejected."""
    with _stub_llm(
        [{"type": "close_task", "target_text": "Close it",
          "targets": [{"id": "t-boonli", "title": "MJ Pay Boonli invoice for June lunches"}],
          "confidence": "high"}],
        "Closed the Boonli invoice task.",
    ):
        client = TestClient(build_app(_server_config(tmp_path)))
        resp = client.post("/synthetic/compose/kavi-reply", json=_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["output"] == "Closed the Boonli invoice task."
    assert data["parsed_intents"][0]["type"] == "close_task"
    assert data["executed"][0]["simulated"] is True
    assert "verdict" not in data
    # Expectation fields never echo back through input_payload.
    assert "expected_intents" not in data["input_payload"]


# ---- verify mode end-to-end (stubbed LLM, real gates + dry-run executors) -----


def test_verify_pass_path(tmp_path) -> None:
    with _stub_llm(
        [{"type": "close_task", "target_text": "Close it",
          "targets": [{"id": "t-boonli", "title": "MJ Pay Boonli invoice for June lunches"}],
          "confidence": "high"}],
        "Closed the Boonli invoice task.",
    ):
        client = TestClient(build_app(_server_config(tmp_path)))
        resp = client.post("/synthetic/verify/kavi-reply", json=_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "PASS", data["failures"]
    assert data["failures"] == []
    assert data["executed"][0]["result"] == "success"
    assert data["model"]


def test_verify_fail_path_wrong_direction_and_template(tmp_path) -> None:
    """A keep-coerced parse + dead template reply fails BOTH directions."""
    with _stub_llm(
        [{"type": "clarify", "target_text": "Close it",
          "targets": [{"id": "t-boonli", "title": "MJ Pay Boonli invoice for June lunches"}],
          "confidence": "low"}],
        "Got it.",
    ):
        client = TestClient(build_app(_server_config(tmp_path)))
        resp = client.post("/synthetic/verify/kavi-reply", json=_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "FAIL"
    gates = {f["gate"] for f in data["failures"]}
    assert "intent_dropped" in gates            # expected close missing
    assert "wrong_direction_resolution" in gates  # forbidden clarify present
    assert "banned_template_reply" in gates


def test_replay_parser_failure_ships_safe_sentence(tmp_path) -> None:
    """Parser None after retry → the replay grades the safe sentence the
    family would actually receive (and intent_dropped fires)."""
    with _stub_llm(None, "unused"):
        result = synthetic_compose.verify_kavi_reply(
            _server_config(tmp_path), _body(),
        )
    assert result["parsed_intents"] == []
    assert result["verdict"] == "FAIL"
    assert "intent_dropped" in {f["gate"] for f in result["failures"]}
    assert result["output"]  # never silence


def test_replay_runs_dry_executors_with_no_graph(tmp_path) -> None:
    """replay passes graph=None into the executors — proof at the replay
    level that dry-run touches no Graph client at all."""
    with _stub_llm(
        [{"type": "qa_drop", "target_text": "drop it",
          "targets": [{"id": "q-1", "title": "MJ Decide on PEPS"}],
          "confidence": "high"}],
        "Dropped the PEPS question — no task created.",
    ):
        result = synthetic_compose.replay_kavi_reply(
            _server_config(tmp_path),
            _body(
                inbound_text="drop it",
                pending_questions=[{"id": "q-1", "task_title_rendered": "MJ Decide on PEPS"}],
                open_tasks=[],
            ),
        )
    assert result["executed"][0] == {
        "intent_type": "qa_drop", "target_ids": ["q-1"],
        "target_titles": ["MJ Decide on PEPS"], "simulated": True,
        "result": "success",
    }
