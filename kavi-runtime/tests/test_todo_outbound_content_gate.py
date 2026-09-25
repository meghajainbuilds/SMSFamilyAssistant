"""Tests for the outbound-content gate on MS To Do task writes
(Fix 1 of the 2026-05-06 audit follow-up).

Both `GraphClient.create_todo_task` (email path) and
`GraphClient.create_task_in_shared_list` (iMessage create-task verb,
manual-missed correction, coordination 4b) now run the rendered title +
body through `outbound_scanner.gate_outbound_content` before the Graph
POST. A sensitive-pattern hit (credit-card / SSN / routing / account
number) raises `OutboundContentBlocked` and writes a row to
outbound_blocked.jsonl with surface="todo_body".

Scope of these tests: just the gate decision. We don't hit Graph; the
gate runs in-process before any HTTP call. A clean payload is exercised
with httpx.post monkeypatched so the test verifies the gate ALLOWS but
without making a real network call.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_todo_outbound_content_gate.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kavi_runtime import graph_client


@pytest.fixture
def cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "graph": {
            "subscription_lifetime_minutes": 4230,
            "subscription_renewal_buffer_minutes": 60,
            "mstodo_shared_list_id": "AQMkADAwTEST==",
        },
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
            "household_md": str(tmp_path / "household.md"),
        },
    }


def _make_client(cfg: dict[str, Any]) -> graph_client.GraphClient:
    """Build a GraphClient without doing real OAuth. We patch the household
    discovery to return a fixed pair, then patch the MSAL machinery so
    construction succeeds without a token cache."""
    with patch.object(
        graph_client, "discover_household_accounts",
        return_value=["megha@example.com"],
    ), patch.object(graph_client, "PublicClientApplication", MagicMock()):
        return graph_client.GraphClient(cfg)


def _read_blocked_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(cfg["paths"]["outbound_blocked_jsonl"])
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ---- create_todo_task (email path) ---------------------------------------


def test_create_todo_task_blocks_card_shaped_title(cfg: dict[str, Any]) -> None:
    """A 16-digit numeric in the LLM-rendered title trips credit_card."""
    g = _make_client(cfg)
    bad_task = {
        "owner": "megha",
        "title": "Pay invoice 4111 1111 1111 1111 today",
        "owner_reason": "ack",
        "confidence": "medium",
    }
    with pytest.raises(graph_client.OutboundContentBlocked):
        g.create_todo_task(
            "AQMkADAwTEST==", bad_task,
            source_email_id="msg-1", source_subject="Invoice",
        )
    rows = _read_blocked_rows(cfg)
    assert any(r.get("surface") == "todo_body" for r in rows)
    assert any("credit_card" in r.get("reason", "") for r in rows)


def test_create_todo_task_blocks_ssn_shaped_body(cfg: dict[str, Any]) -> None:
    """SSN-shaped numeric in `owner_reason` (which renders into body) trips ssn."""
    g = _make_client(cfg)
    bad_task = {
        "owner": "max",
        "title": "Tax return follow-up",
        "owner_reason": "Megha's SSN on the form is 123-45-6789, verify.",
        "confidence": "medium",
    }
    with pytest.raises(graph_client.OutboundContentBlocked):
        g.create_todo_task(
            "AQMkADAwTEST==", bad_task,
            source_email_id="msg-2", source_subject="Tax",
        )
    rows = _read_blocked_rows(cfg)
    assert any("ssn" in r.get("reason", "") for r in rows)


def test_create_todo_task_blocks_routing_shaped_body(cfg: dict[str, Any]) -> None:
    """A standalone 9-digit string in body trips routing_number."""
    g = _make_client(cfg)
    bad_task = {
        "owner": "megha",
        "title": "Send wire to school",
        "owner_reason": "Routing 121000248 per their email.",
        "confidence": "medium",
    }
    with pytest.raises(graph_client.OutboundContentBlocked):
        g.create_todo_task(
            "AQMkADAwTEST==", bad_task,
            source_email_id="msg-3", source_subject="Wire",
        )
    rows = _read_blocked_rows(cfg)
    assert any(
        "routing_number" in r.get("reason", "") or "credit_card" in r.get("reason", "")
        for r in rows
    )


def test_create_todo_task_passes_clean_content(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean rendered title + body proceeds to the Graph POST. We patch
    httpx.post + the access-token call so no network traffic happens."""
    g = _make_client(cfg)

    monkeypatch.setattr(
        graph_client.GraphClient, "_access_token", lambda self, account: "fake-token",
    )

    fake_post = MagicMock()
    fake_post.return_value = MagicMock(
        status_code=201, json=lambda: {"id": "task-123"}, raise_for_status=lambda: None,
    )
    monkeypatch.setattr(graph_client.httpx, "post", fake_post)

    clean_task = {
        "owner": "megha",
        "title": "Pick up Owen from school early",
        "owner_reason": "Field trip rescheduled",
        "confidence": "high",
    }
    task_id = g.create_todo_task(
        "AQMkADAwTEST==", clean_task,
        source_email_id="msg-clean", source_subject="School",
    )
    assert task_id == "task-123"
    # No blocked rows for a clean payload.
    assert _read_blocked_rows(cfg) == []
    fake_post.assert_called()
