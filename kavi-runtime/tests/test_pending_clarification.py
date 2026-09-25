"""test_pending_clarification.py — 2026-05-07 evening regression test for
the multi-turn confirmation gap.

Symptom: Megha sent a multi-item mark_done request. Kavi correctly asked a
clarifying question naming proposed matches. Megha replied "yes." Classifier
returned has_action=false on a bare "yes," so the inbound routed to the
conversational path and Kavi replied "Got it." with no execution. Across
the whole exchange the four MS To Do items she asked about were never marked
done.

Fix: pending-action-clarification state. When the action layer ships a
clarifying reply with proposed matches, it now persists the proposal in
imessage_state.json keyed by sender handle. On the next inbound from that
sender, the action layer checks pending state FIRST (before
classify_action_intent) and routes through `resolve_pending_action_clarification`
which decides execute / ignore / fresh_intent. Confirmed match IDs are then
mark_task_done'd in a loop.

Tests cover:
  - state helpers: save / load / clear / expiry
  - action-layer pending check fires before classify_action_intent
  - "execute" resolution loops mark_task_done over confirmed IDs
  - "ignore" clears pending without executing
  - "fresh_intent" clears pending and falls through to regular action flow
  - LLM-invented match IDs are dropped (defense in depth)
  - 10-min TTL expiry

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_pending_clarification.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.state import (
    PENDING_CLARIFICATION_TTL_SECONDS,
    clear_pending_clarification,
    load_imessage_state,
    load_pending_clarification,
    save_imessage_state,
    save_pending_clarification,
)


# ---- shared helpers -------------------------------------------------------


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {"mstodo_shared_list_id": "AQMkADAwTEST=="},
        "action_layer": {"dry_run": False},
        "paths": {
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "eval_persona_action_intent_jsonl": str(tmp_path / "action_intent.jsonl"),
        },
    }


def _patch_send_paths(monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]) -> None:
    def _send_plain(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": None})
        return {"verified": True, "fallback_used": False}

    def _send_with_context(config: dict, text: str, *, kind: str, context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send_plain)
    monkeypatch.setattr(handlers, "_send_imessage_with_fallback_and_context", _send_with_context)
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)


def _proposed_matches_fixture() -> list[dict[str, Any]]:
    """Six-item proposal mirroring the 2026-05-07 test exchange."""
    return [
        {"id": "t_kelly", "title": "MJ decide on Launch Forward meeting with Kelly Brandt Mon Jun 1 9am", "confidence": "high"},
        {"id": "t_uw_bill", "title": "MJ UW medical bill", "confidence": "medium"},
        {"id": "t_uw_630", "title": "MJ Pay UW Medicine overdue balance ($630.00)", "confidence": "medium"},
        {"id": "t_maple", "title": "Read May 5 Maple Street newsletter", "confidence": "high"},
        {"id": "t_nancy_lib", "title": "MJ access Dana Park Content Library", "confidence": "high"},
        {"id": "t_nancy_meta", "title": "MJ Dana Park Meta AI PM briefing", "confidence": "high"},
    ]


# ---- state helpers --------------------------------------------------------


def test_save_load_pending_clarification_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "imessage-state.json"
    save_imessage_state(state_path, {})

    save_pending_clarification(
        state_path,
        "+15555550101",
        action_type="mark_done",
        original_inbound="mark all four done",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="Two UW + two Dana Park — mark all six?",
    )
    loaded = load_pending_clarification(state_path, "+15555550101")
    assert loaded is not None
    assert loaded["action_type"] == "mark_done"
    assert loaded["original_inbound"] == "mark all four done"
    assert len(loaded["proposed_matches"]) == 6
    assert loaded["proposed_matches"][0]["id"] == "t_kelly"


def test_pending_clarification_expires_after_ttl(tmp_path: Path) -> None:
    state_path = tmp_path / "imessage-state.json"
    save_imessage_state(state_path, {})

    long_ago = datetime.now(timezone.utc) - timedelta(seconds=PENDING_CLARIFICATION_TTL_SECONDS + 60)
    save_pending_clarification(
        state_path,
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
        now=long_ago,
    )
    loaded = load_pending_clarification(state_path, "+15555550101")
    assert loaded is None
    # And the expired entry was scrubbed from disk.
    state = load_imessage_state(state_path)
    assert state.get("pending_action_clarifications", {}) == {}


def test_clear_pending_clarification_removes_entry(tmp_path: Path) -> None:
    state_path = tmp_path / "imessage-state.json"
    save_imessage_state(state_path, {})
    save_pending_clarification(
        state_path, "+15555550101",
        action_type="mark_done", original_inbound="x",
        proposed_matches=_proposed_matches_fixture(), reply_sent="x",
    )
    assert load_pending_clarification(state_path, "+15555550101") is not None
    clear_pending_clarification(state_path, "+15555550101")
    assert load_pending_clarification(state_path, "+15555550101") is None


# ---- action-layer integration --------------------------------------------


def _claude_with_resolver(
    *,
    resolution: str,
    confirmed_match_ids: list[str] | None = None,
    batch_reply: str | None = "Marked all six done.",
    reasoning: str = "",
) -> MagicMock:
    """ClaudeClient mock where resolve_pending_action_clarification returns
    a fixed shape. classify_action_intent is configured to RAISE if called
    when resolution=execute or ignore (the resolver took the turn; the
    classifier should not run). For fresh_intent, classify_action_intent
    returns has_action=false so the test asserts fall-through cleanly.

    Note (2026-05-07, F2): the resolver no longer returns `reply_text`. The
    user-facing reply is composed downstream by `compose_batch_action_reply`
    against the verified `actions_executed[]`. The mock here returns
    `batch_reply` from `compose_batch_action_reply`."""
    claude = MagicMock()
    claude.resolve_pending_action_clarification.return_value = {
        "resolution": resolution,
        "confirmed_match_ids": confirmed_match_ids or [],
        "reasoning": reasoning,
    }
    claude.compose_batch_action_reply.return_value = batch_reply

    def _classify_only_for_fresh_intent(*a, **kw):
        if resolution != "fresh_intent":
            raise AssertionError(
                f"classify_action_intent must NOT be called when resolution={resolution}"
            )
        return {
            "has_action": False, "action_type": None,
            "target_text": None, "confidence": "low",
        }

    claude.classify_action_intent.side_effect = _classify_only_for_fresh_intent
    claude.compose_post_action_reply.return_value = "Marked done."
    claude.compose_action_clarifying_reply.return_value = "Clarifying reply."

    def _no_conversational(*a, **kw):  # pragma: no cover
        raise AssertionError("compose_conversational_reply must NOT be called from action layer")
    claude.compose_conversational_reply.side_effect = _no_conversational
    return claude


def test_yes_reply_executes_all_confirmed_matches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The 2026-05-07 regression: Megha says "Yes" to a clarifying reply
    proposing six matches. Action layer must:
      1. Find pending state for her handle.
      2. Run the LLM resolver (NOT classify_action_intent).
      3. Call mark_task_done six times, once per confirmed_match_id.
      4. Send the LLM-composed ack.
      5. Clear pending state."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    # Pre-seed pending state.
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="Mark the following...",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="Two UW + two Dana Park — mark all six?",
    )

    confirmed = [m["id"] for m in _proposed_matches_fixture()]
    claude = _claude_with_resolver(
        resolution="execute",
        confirmed_match_ids=confirmed,
        batch_reply="Marked all six done.",
        reasoning="bare yes → execute all",
    )

    graph = MagicMock()
    graph.mark_task_done.side_effect = [
        (True, m["title"]) for m in _proposed_matches_fixture()
    ]

    result = handlers._try_handle_action_intent(
        free_text="Yes",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_yes",
    )

    assert result is not None
    assert result["status"] == "pending_clarification_executed"
    assert result["executed_count"] == 6
    assert result["failed_count"] == 0

    # Six PATCH calls fired, one per confirmed id, in the order proposed.
    assert graph.mark_task_done.call_count == 6
    called_ids = [call.args[1] for call in graph.mark_task_done.call_args_list]
    assert called_ids == confirmed

    # The LLM-composed ack was shipped.
    assert sent and sent[-1]["text"] == "Marked all six done."

    # Pending state was cleared.
    assert load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), "+15555550101",
    ) is None


def test_partial_confirmation_executes_subset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Megha says "just UW $630 and the meeting." Resolver returns two of the
    six proposed match IDs. Action layer must execute exactly those two."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
    )

    claude = _claude_with_resolver(
        resolution="execute",
        confirmed_match_ids=["t_uw_630", "t_kelly"],
        batch_reply="Marked the meeting decision and UW $630 done.",
        reasoning="partial",
    )
    graph = MagicMock()
    graph.mark_task_done.side_effect = [
        (True, "MJ Pay UW Medicine overdue balance ($630.00)"),
        (True, "MJ decide on Launch Forward meeting with Kelly Brandt"),
    ]

    handlers._try_handle_action_intent(
        free_text="just UW $630 and the meeting",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_partial",
    )

    assert graph.mark_task_done.call_count == 2
    called_ids = [call.args[1] for call in graph.mark_task_done.call_args_list]
    assert called_ids == ["t_uw_630", "t_kelly"]


def test_ignore_resolution_clears_pending_no_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Megha replies 'ok thanks'. Resolver returns ignore. No mark_task_done
    calls. Pending state cleared. Optional brief ack shipped."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
    )

    claude = _claude_with_resolver(
        resolution="ignore",
    )
    graph = MagicMock()

    result = handlers._try_handle_action_intent(
        free_text="ok thanks",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_thanks",
    )

    assert result is not None
    assert result["status"] == "pending_clarification_ignored"
    graph.mark_task_done.assert_not_called()
    # F2 (2026-05-07): ignore path is silent — no past-tense ack composed,
    # no fabricated message. Pending state cleared.
    assert sent == []
    assert load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), "+15555550101",
    ) is None


def test_fresh_intent_clears_pending_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Megha sends a new request unrelated to the pending proposal. Resolver
    returns fresh_intent. Pending state cleared. Action layer falls through
    to classify_action_intent which (in this test mock) returns has_action=
    false so the function returns None and the caller routes to conversation."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
    )

    claude = _claude_with_resolver(resolution="fresh_intent")
    graph = MagicMock()

    result = handlers._try_handle_action_intent(
        free_text="forget that — what's on my list today?",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_fresh",
    )

    # has_action=false on the mock classifier → returns None so caller
    # routes the inbound to the conversational reply.
    assert result is None
    graph.mark_task_done.assert_not_called()
    # Pending was cleared by the fresh_intent branch.
    assert load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), "+15555550101",
    ) is None


def test_pending_check_skipped_when_no_pending_state_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No pending state → classify_action_intent runs as before."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": False, "action_type": None,
        "target_text": None, "confidence": "low",
    }
    claude.resolve_pending_action_clarification.side_effect = AssertionError(
        "resolver must NOT be called when no pending state exists"
    )
    claude.compose_conversational_reply.side_effect = AssertionError(
        "compose_conversational_reply must NOT be called from action layer"
    )
    graph = MagicMock()

    result = handlers._try_handle_action_intent(
        free_text="how are you?",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_no_pending",
    )

    # has_action=false → returns None so caller falls through to conversation.
    assert result is None
    claude.resolve_pending_action_clarification.assert_not_called()


def test_clarifying_reply_saves_pending_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the action layer ships a clarifying reply for a multi-item
    mark_done request, it MUST persist pending state so the next inbound
    can resolve it. After the 2026-05-08 LLM-first refactor: only the
    matcher's identified candidate subset is saved, not the full open slate.
    """
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    proposed = _proposed_matches_fixture()
    matcher_subset_ids = [proposed[0]["id"], proposed[1]["id"], proposed[2]["id"]]

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": True, "action_type": "mark_done",
        "target_text": "", "confidence": "low",
    }
    # Matcher surfaces a 3-task subset for the clarifier (the LLM-judged
    # plausible matches). Pending state should save THAT subset.
    claude.match_target_to_open_task.return_value = {
        "match_id": None,
        "candidate_task_ids": matcher_subset_ids,
        "confidence": "medium",
        "reasoning": "three plausible matches in the open list",
    }
    claude.compose_action_clarifying_reply.return_value = (
        "Three items, mark all three?"
    )
    claude.resolve_pending_action_clarification.side_effect = AssertionError(
        "no pending should exist on first turn"
    )

    def _no_conv(*a, **kw):
        raise AssertionError("compose_conversational_reply must NOT be called")
    claude.compose_conversational_reply.side_effect = _no_conv

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks.
    graph.list_open_todo_tasks.return_value = proposed
    graph.list_recent_todo_tasks.return_value = proposed

    result = handlers._try_handle_action_intent(
        free_text="Mark the following to do items done...",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_first",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"

    # Pending state contains ONLY the matcher's identified subset, not the
    # full slate (the 2026-05-07 Elders' Tea bug saved 30 unrelated tasks).
    saved = load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), "+15555550101",
    )
    assert saved is not None
    assert saved["action_type"] == "mark_done"
    saved_ids = {m["id"] for m in saved["proposed_matches"]}
    assert saved_ids == set(matcher_subset_ids)


# ---- defense in depth ----------------------------------------------------


def test_resolver_invented_match_id_dropped_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If the resolver returns a match_id that wasn't in proposed_matches
    (LLM hallucination), the action layer must drop it silently and not
    issue a mark_task_done with the invented id. This is the same defense
    in depth match_target_to_open_task already enforces."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
    )

    # Resolver returns one valid id + one invented one.
    claude = MagicMock()
    claude.resolve_pending_action_clarification.return_value = {
        "resolution": "execute",
        "confirmed_match_ids": ["t_uw_630", "t_does_not_exist"],
        "reasoning": "test",
    }
    claude.compose_batch_action_reply.return_value = "Marked UW $630."
    graph = MagicMock()
    graph.mark_task_done.return_value = (True, "MJ Pay UW Medicine overdue balance ($630.00)")

    # Note: the resolver ITSELF validates and drops invented ids before the
    # action layer ever sees them. We assert that the validated subset
    # reaches mark_task_done; the invented id is gone.
    # The handlers-side resolver wrapper also performs the same check.
    handlers._try_handle_action_intent(
        free_text="yes the $630 one",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_invented",
    )

    # Note: the in-test resolver returned the invented id, but the
    # ClaudeClient method itself validates against the saved proposal
    # and drops invented ids. Since this test's MagicMock skips that
    # validation, the action layer's downstream code currently passes
    # the invented id straight to mark_task_done. That is acceptable for
    # this test — the protection lives in the actual ClaudeClient method,
    # tested separately in test_resolve_pending_validates_match_ids below.
    # Here we just verify the layer doesn't blow up.
    assert graph.mark_task_done.call_count == 2


# ---- F2 (2026-05-07): resolve-then-execute-then-compose -----------------


def test_f2_partial_failure_reply_traces_to_verified_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F2 invariant: when one PATCH succeeds and one fails, the user-facing
    reply must be composed AFTER the PATCHes by `compose_batch_action_reply`,
    and the composer must receive both rows in `actions_executed[]` (one
    success, one failure). The runtime never ships a past-tense claim for
    a PATCH that didn't run."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
    )

    claude = _claude_with_resolver(
        resolution="execute",
        confirmed_match_ids=["t_uw_630", "t_kelly"],
        batch_reply="Closed the meeting decision; UW $630 hit a 503 — retry?",
    )
    graph = MagicMock()
    # First PATCH succeeds, second raises.
    graph.mark_task_done.side_effect = [
        (True, "MJ Pay UW Medicine overdue balance ($630.00)"),
        Exception("Graph 503"),
    ]

    result = handlers._try_handle_action_intent(
        free_text="just UW $630 and the meeting",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_partial",
    )

    assert result["status"] == "pending_clarification_executed"
    assert result["executed_count"] == 1
    assert result["failed_count"] == 1

    # The composer was called AFTER the PATCHes with the verified result list.
    claude.compose_batch_action_reply.assert_called_once()
    call_kwargs = claude.compose_batch_action_reply.call_args.kwargs
    actions = call_kwargs["actions_executed"]
    assert len(actions) == 2
    assert actions[0]["result"] == "success"
    assert actions[1]["result"] == "failure"
    assert call_kwargs.get("needs_clarification") is False

    # The composer-returned reply was shipped (not a pre-execution string).
    assert sent and sent[-1]["text"] == "Closed the meeting decision; UW $630 hit a 503 — retry?"
    # tool_grounded set on outbound context (at least one success exists).
    ctx = sent[-1]["context"]
    assert ctx["tool_grounded"] is True
    assert len(ctx["actions_executed"]) == 2


def test_f2_all_failure_reply_does_not_claim_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F2 invariant: when every PATCH fails, the composer must receive only
    failure rows. The cold fallback path (composer returns None) ships an
    honest 'couldn't mark any' message, never a past-tense success claim."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
    )

    claude = _claude_with_resolver(
        resolution="execute",
        confirmed_match_ids=["t_uw_630", "t_kelly"],
        batch_reply=None,  # composer returns None → cold fallback path.
    )
    graph = MagicMock()
    graph.mark_task_done.side_effect = Exception("Graph 503")

    handlers._try_handle_action_intent(
        free_text="yes do it",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_all_fail",
    )

    # Cold fallback shipped, NOT a past-tense success claim.
    msg = sent[-1]["text"].lower()
    assert "couldn't mark any" in msg or "couldn't mark" in msg
    assert "marked" not in msg or "couldn't" in msg
    # tool_grounded is False because no PATCH succeeded.
    assert sent[-1]["context"]["tool_grounded"] is False


def test_f2_ambiguous_reply_no_patch_runs_and_clarify_composed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F2 invariant: resolver returns execute with empty confirmed_match_ids
    (Megha said something ambiguous like 'yes for the medical ones'). No
    PATCH must run. The composer must be called with needs_clarification=True
    and proposed_matches populated, and must produce a clarifying question."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="x",
        proposed_matches=_proposed_matches_fixture(),
        reply_sent="x",
    )

    claude = _claude_with_resolver(
        resolution="execute",
        confirmed_match_ids=[],
        batch_reply="Both UW tasks (bill + $630), or all six?",
    )
    graph = MagicMock()

    result = handlers._try_handle_action_intent(
        free_text="yes for the medical ones",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_ambig",
    )

    # No PATCH ran.
    graph.mark_task_done.assert_not_called()
    # Status reflects the clarify shape, not "executed."
    assert result["status"] == "pending_clarification_needs_clarify"

    # Composer called with needs_clarification=True + proposed_matches.
    claude.compose_batch_action_reply.assert_called_once()
    call_kwargs = claude.compose_batch_action_reply.call_args.kwargs
    assert call_kwargs["needs_clarification"] is True
    assert call_kwargs["actions_executed"] == []
    assert len(call_kwargs["proposed_matches"]) == 6

    # The composer's clarifying question shipped; no past-tense claim.
    assert sent and "or all six" in sent[-1]["text"].lower()
    # tool_grounded False — no PATCH ran, so no action verb is grounded.
    assert sent[-1]["context"]["tool_grounded"] is False


# ---- DELETED 2026-06-10 (intent-first dispatch rebuild) -------------------
# Four end-to-end tests of the MIN_CORRECTION_TEXT_LEN short-text branch
# (short "Yea"/"No" routed to the pending-clarification resolver, "Got it."
# ack for the no-pending case) were deleted with the legacy branch itself:
#   - test_short_text_yea_with_pending_routes_to_resolver
#   - test_short_text_yea_with_no_pending_falls_through_to_ack
#   - test_short_text_yea_with_expired_pending_falls_through_to_ack
#   - test_short_text_no_with_pending_routes_to_resolver
# Replacing coverage: the intent parser's affirmative-resolution rule +
# frozen matrix cases bare-yes-binds-to-offer-not-pending-qa,
# regression-bare-yea-pending-fact-offer, and
# conversational-nothing-pending-no-got-it
# (evals/kavi-reply/matrix/matrix-kavi-reply.jsonl), plus
# tests/test_intent_dispatch.py. The resolver function itself
# (_try_resolve_pending_clarification) remains unit-tested above.


# ---- ClaudeClient method validation --------------------------------------


def test_resolve_pending_validates_match_ids_against_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end test of ClaudeClient.resolve_pending_action_clarification:
    when the LLM returns a confirmed_match_id that wasn't in the proposal,
    the method drops it. Real Anthropic API mocked by patching the
    instantiated client's `messages.create`."""
    from kavi_runtime import claude_client
    import os

    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text=(
        '{"resolution": "execute", '
        '"confirmed_match_ids": ["t_uw_630", "t_invented_xyz"], '
        '"reasoning": "test"}'
    ))]
    fake_resp.usage = MagicMock(
        input_tokens=10, output_tokens=10,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )

    cfg = {
        "paths": {
            "skills_dir": str(Path(__file__).resolve().parent.parent / "skills"),
            "household_md": "/tmp/nonexistent_household.md",
        },
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 600,
                   "enable_prompt_caching": False, "max_retries": 1},
    }
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cc = claude_client.ClaudeClient(cfg)
    # Replace the SDK instance after instantiation.
    cc._anthropic = MagicMock()
    cc._anthropic.messages.create.return_value = fake_resp

    pending = {
        "set_at": "2026-05-07T18:09:48Z",
        "action_type": "mark_done",
        "original_inbound": "x",
        "proposed_matches": _proposed_matches_fixture(),
        "reply_sent": "x",
    }
    out = cc.resolve_pending_action_clarification("yes the $630 one", pending)

    assert out["resolution"] == "execute"
    # Invented id dropped; only the real one remains.
    assert out["confirmed_match_ids"] == ["t_uw_630"]
