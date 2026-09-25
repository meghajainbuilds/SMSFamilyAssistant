"""Tests for pending-facts surfacing in periodic_summary.

Original (2026-05-06): a deterministic f-string suffix appended "N pending
facts to review (open pending_facts.jsonl on Kavi's Mac)" to the LLM-
composed summary. That string leaked a developer-facing file path into
Megha's iMessage and violated kavi-persona Principle 7 (output layer is
always LLM-composed).

Fix 4 (2026-05-07): the runtime now reads top-3 pending facts (by recency,
status=pending_confirmation) and passes them as context to the LLM
composer. The composer phrases them in voice: "I have 3 facts to confirm:
Rosa cash, beans, slip. Want them now?" — never as a path or count alone.

This test file covers:
  - The count helper still filters by status (legacy assertion).
  - The new top-N reader returns rows newest-first with topic + snippet.
  - The runtime no longer composes the f-string suffix.
  - When pending_facts_count > 0 with no rows in pending_confirmation
    status, no surfacing fires.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_pending_facts_surfacing.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers


@pytest.fixture
def cfg_with_pending(tmp_path: Path) -> dict[str, Any]:
    pending_path = tmp_path / "pending_facts.jsonl"
    rows = [
        {
            "pending_id": "p1",
            "ts": "2026-05-06T18:00:00Z",
            "fact_text": "Max said he'll grab cash for Rosa tomorrow",
            "scope": "max",
            "source_decision_id": None,
            "expires_at": None,
            "status": "pending_confirmation",
            "inbound_source": "imessage from Max",
            "todo": "TODO: Megha to confirm",
        },
        {
            "pending_id": "p2",
            "ts": "2026-05-06T18:01:00Z",
            "fact_text": "Nadia asked us to soak black beans Mon night",
            "scope": "household",
            "source_decision_id": None,
            "expires_at": None,
            "status": "pending_confirmation",
            "inbound_source": "imessage from Nadia",
            "todo": "TODO: Megha to confirm",
        },
        # Non-pending row to verify the count filter actually filters.
        {
            "pending_id": "p3",
            "ts": "2026-05-06T18:02:00Z",
            "fact_text": "old already-confirmed fact",
            "scope": "household",
            "source_decision_id": None,
            "expires_at": None,
            "status": "confirmed",
            "inbound_source": "manual",
            "todo": "",
        },
    ]
    pending_path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    return {
        "paths": {
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(pending_path),
        },
    }


@pytest.fixture
def cfg_no_pending(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
        },
    }


def test_count_pending_facts_filters_by_status(
    cfg_with_pending: dict[str, Any],
) -> None:
    """The helper counts ONLY rows with status=pending_confirmation;
    confirmed / dropped rows are excluded so the line on the digest
    reflects what's actually awaiting Megha's review."""
    count = handlers._count_pending_facts(cfg_with_pending)
    assert count == 2


def test_count_pending_facts_returns_zero_when_file_missing(
    cfg_no_pending: dict[str, Any],
) -> None:
    count = handlers._count_pending_facts(cfg_no_pending)
    assert count == 0


# ---- Fix 4 (2026-05-07): voice-not-path surfacing ------------------------


def test_read_pending_facts_for_summary_returns_top_n_newest_first(
    cfg_with_pending: dict[str, Any],
) -> None:
    """The new reader surfaces newest pending_confirmation rows first,
    capped at top_n. Each row projects to {topic, snippet} so the LLM
    composer can phrase them in voice."""
    rows = handlers._read_pending_facts_for_summary(cfg_with_pending, top_n=3)
    # Two pending rows in the fixture (p1 + p2; p3 is confirmed, excluded).
    assert len(rows) == 2
    # Newest first: p2 has ts 18:01:00Z, p1 has 18:00:00Z.
    assert "beans" in rows[0]["snippet"].lower()
    assert "cash" in rows[1]["snippet"].lower()
    # Topic surfaces the source name when present.
    for r in rows:
        assert "topic" in r and r["topic"]
        assert "snippet" in r and r["snippet"]


def test_read_pending_facts_top_n_caps(cfg_with_pending: dict[str, Any]) -> None:
    """top_n caps the surfaced count even when more pending rows exist."""
    rows = handlers._read_pending_facts_for_summary(cfg_with_pending, top_n=1)
    assert len(rows) == 1


def test_read_pending_facts_returns_empty_when_file_missing(
    cfg_no_pending: dict[str, Any],
) -> None:
    rows = handlers._read_pending_facts_for_summary(cfg_no_pending)
    assert rows == []


def test_periodic_summary_no_longer_leaks_file_path(
    monkeypatch: pytest.MonkeyPatch, cfg_with_pending: dict[str, Any], tmp_path: Path,
) -> None:
    """Fix 4: the periodic_summary outbound must NOT contain the literal
    text 'pending_facts.jsonl' or 'open the file' or any developer-facing
    path string. The runtime now feeds context to the LLM composer; the
    composer phrases pending facts in voice."""
    # Stub state/queue paths; only the pending-facts file is real.
    cfg = dict(cfg_with_pending)
    cfg["paths"] = dict(cfg["paths"])
    cfg["paths"]["imessage_state"] = str(tmp_path / "imessage-state.json")
    cfg["paths"]["runs_jsonl"] = str(tmp_path / "runs.jsonl")
    cfg["imessage"] = {"megha_phone": "+15555550101"}
    cfg["graph"] = {"mstodo_shared_list_id": "AQTEST=="}

    sent: list[str] = []

    def _send(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append(text)
        return {"verified": True, "fallback_used": False}

    from capabilities.kavi_persona.composers import periodic_summary as _ps_module
    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send)
    monkeypatch.setattr(_ps_module, "_send_imessage_with_fallback", _send)

    claude = MagicMock()
    # The composer phrases pending facts in voice.
    claude.compose_periodic_summary.return_value = (
        "I have 2 facts to confirm: Max, Nadia. Want them now?"
    )
    monkeypatch.setattr(
        handlers, "_get_clients",
        lambda c: (MagicMock(), claude, MagicMock()),
    )
    monkeypatch.setattr(
        _ps_module, "_get_clients",
        lambda c: (MagicMock(), claude, MagicMock()),
    )

    handlers.periodic_summary(cfg, is_rollup=True)

    # The composer was given pending_facts as a kwarg (top-3 by recency,
    # newest first).
    claude.compose_periodic_summary.assert_called_once()
    kwargs = claude.compose_periodic_summary.call_args.kwargs
    assert "pending_facts" in kwargs
    # Scope-aware routing (2026-06-23): the scope:"max" fact ("Max said he'll
    # grab cash for Rosa") must NOT reach Megha's digest — only the
    # household fact (Nadia / black beans) does. This is the Premier-
    # Mechanical leak fix: a Max-scoped coordination commitment never lands
    # in Megha's summary.
    assert len(kwargs["pending_facts"]) == 1
    surfaced = " ".join(
        (f.get("snippet") or "") + " " + (f.get("topic") or "")
        for f in kwargs["pending_facts"]
    ).lower()
    assert "beans" in surfaced or "nadia" in surfaced, surfaced
    assert "rosa" not in surfaced and "cash" not in surfaced, surfaced
    # Outbound must NOT contain the leaked file path / dev-facing strings.
    assert sent, "periodic_summary should have shipped a message"
    body = sent[-1]
    assert "pending_facts.jsonl" not in body
    assert "open the file" not in body.lower()
    assert "kavi's mac" not in body.lower()


def test_periodic_summary_no_pending_facts_omits_surface(
    monkeypatch: pytest.MonkeyPatch, cfg_no_pending: dict[str, Any], tmp_path: Path,
) -> None:
    """When pending_facts is empty, the composer is called with an empty
    list and no surfacing fires anywhere."""
    cfg = dict(cfg_no_pending)
    cfg["paths"] = dict(cfg["paths"])
    cfg["paths"]["imessage_state"] = str(tmp_path / "imessage-state.json")
    cfg["paths"]["runs_jsonl"] = str(tmp_path / "runs.jsonl")
    cfg["imessage"] = {"megha_phone": "+15555550101"}
    cfg["graph"] = {"mstodo_shared_list_id": "AQTEST=="}

    sent: list[str] = []
    from capabilities.kavi_persona.composers import periodic_summary as _ps_module
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback",
        lambda c, t, *, kind, **kw: (sent.append(t), {"verified": True, "fallback_used": False})[1],
    )
    monkeypatch.setattr(
        _ps_module, "_send_imessage_with_fallback",
        lambda c, t, *, kind, **kw: (sent.append(t), {"verified": True, "fallback_used": False})[1],
    )

    claude = MagicMock()
    claude.compose_periodic_summary.return_value = (
        "Quiet day. 0 new tasks, 0 pending. Tomorrow we go again."
    )
    monkeypatch.setattr(
        handlers, "_get_clients",
        lambda c: (MagicMock(), claude, MagicMock()),
    )
    monkeypatch.setattr(
        _ps_module, "_get_clients",
        lambda c: (MagicMock(), claude, MagicMock()),
    )

    handlers.periodic_summary(cfg, is_rollup=True)

    kwargs = claude.compose_periodic_summary.call_args.kwargs
    assert kwargs["pending_facts"] == []
    body = sent[-1]
    assert "facts to confirm" not in body.lower()
    assert "pending_facts.jsonl" not in body
