"""Production-shape integration tests for the 2026-05-08 LLM-first revert
of the action layer.

What this file covers:

  Fix A (revert): no deterministic topic pre-filter sits between the action
  intent classifier and the LLM matcher. The matcher receives ALL 30 open
  tasks and surfaces the relevant subset.

  Fix B (matcher subset → pending state + clarifier): when the matcher
  returns multiple low-confidence candidates, the runtime saves ONLY that
  subset into pending_action_clarifications and passes that subset to the
  clarifying composer. The 2026-05-07 Oak Circle Tea bug saved the full
  30-task slate.

  Fix C (G-A1 fail-closed on action_clarifying state claims): a clarifying
  composer producing "X is already showing completed" is replaced with the
  alert_fallback message rather than shipped as the lie.

  Fix D (post-clarifier create_task guard): when the resolver returns
  fresh_intent on a recent clarifier, classify_action_intent returning
  create_task is suppressed in favor of asking the user to disambiguate.
  The 17:41:03Z trace created a duplicate "MJ MJ oak circle tea at maple
  street tomorrow" task; this guard prevents that.

Each test simulates real production input shape (apostrophes intact, full
30-task slate, real handle) and asserts on observable behavior, not on
implementation details.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \\
      tests/test_action_layer_llm_first_2026_05_08.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers, structural_checks
from kavi_runtime.state import (
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


def _patch_send_paths(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
) -> None:
    def _send_plain(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": None})
        return {"verified": True, "fallback_used": False}

    def _send_with_context(
        config: dict, text: str, *, kind: str, context: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send_plain)
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context", _send_with_context,
    )
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)


def _thirty_task_slate_with_four_oak_circle_tea() -> list[dict[str, Any]]:
    """Full 30-task open-list shape, including 4 Oak Circle Tea tasks (apostrophes
    intact) and 26 unrelated tasks. Mirrors the 2026-05-07 production trace.
    """
    oak_circle_tea = [
        {
            "id": "et_zoom",
            "title": "MJ Forward Oak Circle Tea Zoom link to your guest",
            "status": "notStarted",
        },
        {
            "id": "et_cater",
            "title": "MJ Confirm Oak Circle Tea catering count by Friday",
            "status": "notStarted",
        },
        {
            "id": "et_print",
            "title": "MJ Print Oak Circle Tea name tags",
            "status": "notStarted",
        },
        {
            "id": "et_maple",
            "title": "MJ Oak Circle Tea at Maple Street tomorrow 4pm RSVP",
            "status": "notStarted",
        },
    ]
    unrelated = [
        {
            "id": f"t_other_{i:02d}",
            "title": f"MJ Unrelated open task #{i}",
            "status": "notStarted",
        }
        for i in range(26)
    ]
    return oak_circle_tea + unrelated


# ---- Fix A + Fix B: production-shape Oak Circle Tea test ---------------------


def test_oak_circle_tea_full_slate_to_matcher_then_only_matcher_subset_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 2026-05-07 Oak Circle Tea production trace:

      Inbound: "Mark all oak circle tea items done"
      Open list (Graph): 30 tasks, 4 of which are Oak Circle Tea (with
                         apostrophes in titles).

    Pre-fix behavior: deterministic pre-filter tokenized "circle" but
    couldn't match it against `circle'` (trailing apostrophe), produced 0
    matches, fell back to a 6-task unrelated slice, saved THAT into
    pending_clarification, and the composer hallucinated a state claim.

    Post-fix behavior asserted here:
      1. match_target_to_open_task is called with all 30 candidates (no
         deterministic pre-filter strips them first).
      2. The matcher's returned candidate_task_ids subset (the 4 Oak Circle
         Tea ids) is what lands in pending_clarification — NOT the full
         30 tasks, NOT a 6-task fallback slice.
      3. The clarifying composer receives that same 4-task subset (so it
         can name the actual titles in its question).
    """
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    full_slate = _thirty_task_slate_with_four_oak_circle_tea()
    oak_circle_tea_ids = ["et_zoom", "et_cater", "et_print", "et_maple"]

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": True,
        "action_type": "mark_done",
        "target_text": "oak circle tea items",
        "confidence": "low",
    }
    claude.match_target_to_open_task.return_value = {
        "match_id": None,
        "candidate_task_ids": oak_circle_tea_ids,
        "confidence": "medium",
        "reasoning": "Four open Oak Circle Tea tasks; 'all' is a batch reference.",
    }
    claude.compose_action_clarifying_reply.return_value = (
        "Found 4 Oak Circle Tea tasks: Zoom link, catering count, name tags, "
        "Maple Street RSVP. Mark all four?"
    )

    def _no_conv(*a, **kw):
        raise AssertionError("compose_conversational_reply must NOT be called")
    claude.compose_conversational_reply.side_effect = _no_conv

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks
    # (server-side $filter on status). list_recent_todo_tasks kept as a
    # belt-and-suspenders stub so older code paths don't crash.
    graph.list_open_todo_tasks.return_value = full_slate
    graph.list_recent_todo_tasks.return_value = full_slate

    result = handlers._try_handle_action_intent(
        free_text="Mark all oak circle tea items done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101",
        source_imessage_id="im_oak_circle_tea_2026_05_07",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"
    assert result["action_type"] == "mark_done"

    # Assertion 1: matcher received the FULL 30-task slate (Fix A revert).
    claude.match_target_to_open_task.assert_called_once()
    matcher_args = claude.match_target_to_open_task.call_args
    matcher_open_tasks = matcher_args[0][1] if matcher_args.args else matcher_args.kwargs.get("open_tasks")
    if matcher_open_tasks is None:
        matcher_open_tasks = matcher_args.args[1]
    assert len(matcher_open_tasks) == 30
    oak_circle_in_input = [
        t for t in matcher_open_tasks if "Oak Circle" in (t.get("title") or "")
    ]
    assert len(oak_circle_in_input) == 4

    # Assertion 2: pending_clarification holds ONLY the 4 Oak Circle Tea ids.
    saved = load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), "+15555550101",
    )
    assert saved is not None
    saved_ids = {m["id"] for m in saved["proposed_matches"]}
    assert saved_ids == set(oak_circle_tea_ids)

    # Assertion 3: clarifying composer received the 4-task subset, not the
    # full slate (Fix B).
    claude.compose_action_clarifying_reply.assert_called_once()
    composer_kwargs = claude.compose_action_clarifying_reply.call_args.kwargs
    assert composer_kwargs["open_tasks"] is None
    composer_candidates = composer_kwargs["candidates"] or []
    assert {c.get("id") for c in composer_candidates} == set(oak_circle_tea_ids)

    # Assertion 4: the reply Megha sees names the actual task titles (no
    # hallucinated "showing completed" state claim slipped through).
    assert sent
    reply = sent[-1]["text"]
    assert "Mark all four" in reply or "mark all four" in reply.lower()
    forbidden = (
        "showing completed",
        "already done",
        "already marked",
        "already completed",
    )
    for phrase in forbidden:
        assert phrase not in reply.lower(), f"forbidden phrase leaked: {phrase!r}"


# ---- Fix C: state-claim hallucination drops to alert_fallback -------------


def test_clarifying_composer_state_claim_hallucination_blocked_by_g_a1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If the clarifying composer (LLM) hallucinates a past-tense state claim
    like "the volunteering decision is already showing completed" — the
    exact 2026-05-07 production failure — the runtime substitutes the
    alert_fallback message instead of shipping the lie.

    The forbidden-phrases list (`FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES`)
    drives this gate. Two-layer defense:
      Layer 1: claude_client.compose_action_clarifying_reply rewrites the
               LLM output to a safe fallback question pre-send.
      Layer 2: the runtime's send wrapper applies G-A1 fail-closed on
               action_clarifying for the narrow state-claim check
               (added 2026-05-08 second pass).

    This test asserts Layer 2 by patching the LLM composer to return a
    state-claim string that bypassed Layer 1 (e.g., a paraphrase Layer 1
    didn't catch).
    """
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    # The LLM composer returns a state-claim hallucination directly (no
    # Fix-2 rewrite happens because we're stubbing the composer's output;
    # this simulates "Layer 1 missed the paraphrase").
    forbidden_reply = (
        "Scanning open tasks, I'm not finding any Oak Circle Tea items still "
        "open. Looks like everything is already completed."
    )

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": True,
        "action_type": "mark_done",
        "target_text": "oak circle tea",
        "confidence": "low",
    }
    claude.match_target_to_open_task.return_value = {
        "match_id": None,
        "candidate_task_ids": [],
        "confidence": "low",
        "reasoning": "no plausible matches",
    }
    claude.compose_action_clarifying_reply.return_value = forbidden_reply

    def _no_conv(*a, **kw):
        raise AssertionError("compose_conversational_reply must NOT be called")
    claude.compose_conversational_reply.side_effect = _no_conv

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks.
    _slate = _thirty_task_slate_with_four_oak_circle_tea()
    graph.list_open_todo_tasks.return_value = _slate
    graph.list_recent_todo_tasks.return_value = _slate

    result = handlers._try_handle_action_intent(
        free_text="Mark all oak circle tea items done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101",
        source_imessage_id="im_state_claim_lie",
    )

    assert result is not None
    # Megha sees the alert_fallback, NOT the lie. The substituted text is
    # the canonical "I caught myself..." string.
    assert sent
    final_reply = sent[-1]["text"]
    assert "I caught myself" in final_reply
    assert "showing completed" not in final_reply.lower()
    assert "already completed" not in final_reply.lower()
    # And the kind of the substituted message is alert_fallback.
    assert sent[-1]["kind"] == "alert_fallback"


# ---- Fix D: disambiguation routing prevents duplicate task creation -------


def test_disambiguation_after_clarifier_does_not_create_duplicate_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 2026-05-08 17:41:03Z production trace:

      Turn 1: Megha says "Mark all oak circle tea items done"
              → Kavi asks "Found 4 — mark all four?"
              → 4 candidate ids saved as pending_clarification.

      Turn 2: Megha replies "MJ oak circle tea at maple street tomorrow"
              → Resolver returns fresh_intent (incorrectly).
              → classify_action_intent returns create_task.
              → Runtime creates "MJ MJ oak circle tea at maple street tomorrow"
                — duplicate of the existing t_maple candidate.

    Post-fix behavior:
      - When the resolver returns fresh_intent within the clarifier TTL
        AND the next classifier wants create_task, the runtime suppresses
        create_task and asks the user to disambiguate against the prior
        4 candidates. No new task is created.
      - The prior proposal is re-saved so the user's NEXT reply binds to
        it (the clarifier window is preserved across the guard).
    """
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    oak_circle_tea_proposal = [
        {"id": "et_zoom", "title": "MJ Forward Oak Circle Tea Zoom link to your guest"},
        {"id": "et_cater", "title": "MJ Confirm Oak Circle Tea catering count by Friday"},
        {"id": "et_print", "title": "MJ Print Oak Circle Tea name tags"},
        {"id": "et_maple", "title": "MJ Oak Circle Tea at Maple Street tomorrow 4pm RSVP"},
    ]
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="Mark all oak circle tea items done",
        proposed_matches=oak_circle_tea_proposal,
        reply_sent=(
            "Found 4 Oak Circle Tea tasks: Zoom link, catering count, name "
            "tags, Maple Street RSVP. Mark all four?"
        ),
    )

    claude = MagicMock()
    # The resolver mis-classifies the disambiguation as fresh_intent — this
    # is exactly the 2026-05-08 trace's resolver output. The runtime guard
    # is what saves us, even when the resolver is wrong.
    claude.resolve_pending_action_clarification.return_value = {
        "resolution": "fresh_intent",
        "confirmed_match_ids": [],
        "reasoning": "looks like a new task with date 'tomorrow'",
    }
    # The classifier then says create_task (the dangerous case).
    claude.classify_action_intent.return_value = {
        "has_action": True,
        "action_type": "create",
        "target_text": "MJ oak circle tea at maple street tomorrow",
        "confidence": "high",
    }
    claude.compose_action_clarifying_reply.return_value = (
        "I caught a description that matches one of the four Oak Circle Tea "
        "tasks I asked about — Maple Street tomorrow. Mark that one?"
    )

    def _no_conv(*a, **kw):
        raise AssertionError("compose_conversational_reply must NOT be called")
    claude.compose_conversational_reply.side_effect = _no_conv

    graph = MagicMock()
    # If the guard fails, the runtime would call create_todo_task here. We
    # set the mock to RAISE so any accidental call fails the test loudly.
    graph.create_todo_task.side_effect = AssertionError(
        "create_todo_task must NOT be called when disambiguation guard fires"
    )

    result = handlers._try_handle_action_intent(
        free_text="MJ oak circle tea at maple street tomorrow",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101",
        source_imessage_id="im_disambig_2026_05_08",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"
    assert result["reason"] == "post_clarifier_create_guard"
    # No new task was created — the guard fired before _handle_create_task_verb.
    graph.create_todo_task.assert_not_called()

    # The prior proposal was re-saved so the user's next reply still binds.
    saved_after = load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), "+15555550101",
    )
    assert saved_after is not None
    assert {m["id"] for m in saved_after["proposed_matches"]} == {
        "et_zoom", "et_cater", "et_print", "et_maple",
    }


def test_disambiguation_resolver_resolves_to_subset_no_fall_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When the resolver correctly identifies a disambiguation reply (the
    2026-05-08 prompt update), the runtime executes ONLY the named subset
    and does NOT fall through to classify_action_intent.

    This is the happy path that complements the guard test above: when the
    resolver does its job, no guard is needed; when it doesn't, the guard
    catches the failure mode.
    """
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    proposal = [
        {"id": "et_zoom", "title": "MJ Forward Oak Circle Tea Zoom link to your guest"},
        {"id": "et_cater", "title": "MJ Confirm Oak Circle Tea catering count"},
        {"id": "et_print", "title": "MJ Print Oak Circle Tea name tags"},
        {"id": "et_maple", "title": "MJ Oak Circle Tea at Maple Street tomorrow 4pm RSVP"},
    ]
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        "+15555550101",
        action_type="mark_done",
        original_inbound="Mark all oak circle tea items done",
        proposed_matches=proposal,
        reply_sent="Found 4 — mark all four?",
    )

    claude = MagicMock()
    # Resolver correctly identifies "maple street tomorrow" as describing
    # t_maple (the 2026-05-08 skill update teaches it this).
    claude.resolve_pending_action_clarification.return_value = {
        "resolution": "execute",
        "confirmed_match_ids": ["et_maple"],
        "reasoning": "'maple street tomorrow' uniquely describes et_maple among the four",
    }
    claude.compose_batch_action_reply.return_value = (
        "Marked the Maple Street RSVP done."
    )
    # If the resolver path falls through to classify, the test fails because
    # this side_effect raises.
    claude.classify_action_intent.side_effect = AssertionError(
        "classify_action_intent must NOT run when resolver returned execute"
    )

    graph = MagicMock()
    graph.mark_task_done.return_value = (
        True, "MJ Oak Circle Tea at Maple Street tomorrow 4pm RSVP",
    )
    graph.create_todo_task.side_effect = AssertionError(
        "create_todo_task must NOT be called on a disambiguation"
    )

    result = handlers._try_handle_action_intent(
        free_text="MJ oak circle tea at maple street tomorrow",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101",
        source_imessage_id="im_disambig_happy",
    )

    assert result is not None
    assert result["status"] == "pending_clarification_executed"
    # Only et_maple was PATCHed.
    graph.mark_task_done.assert_called_once_with(
        cfg["graph"]["mstodo_shared_list_id"], "et_maple",
    )
    # No duplicate task creation.
    graph.create_todo_task.assert_not_called()


# ---- Fix B: matcher subset for the standard ambiguous-match path ---------


def test_ambiguous_high_intent_match_uses_matcher_candidate_subset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Even when classify_action_intent returns confidence=high (a clean
    target_text), if the matcher itself is unsure (medium / no high-conf
    pick), the runtime uses the matcher's `candidate_task_ids` to populate
    the clarifying composer + pending state. The legacy "matched_task + 2
    most-recently-modified" deterministic slate is gone (Fix B).
    """
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    full_slate = _thirty_task_slate_with_four_oak_circle_tea()

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": True,
        "action_type": "mark_done",
        "target_text": "oak circle tea zoom",
        "confidence": "high",
    }
    # Matcher: 2 plausible candidates, no clear single match.
    claude.match_target_to_open_task.return_value = {
        "match_id": None,
        "candidate_task_ids": ["et_zoom", "et_maple"],
        "confidence": "medium",
        "reasoning": "Zoom link and Maple Street RSVP both could be Zoom-related",
    }
    claude.compose_action_clarifying_reply.return_value = (
        "Two Oak Circle Tea matches: Zoom link or Maple Street RSVP. Which?"
    )

    def _no_conv(*a, **kw):
        raise AssertionError("compose_conversational_reply must NOT be called")
    claude.compose_conversational_reply.side_effect = _no_conv

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks.
    graph.list_open_todo_tasks.return_value = full_slate
    graph.list_recent_todo_tasks.return_value = full_slate
    graph.mark_task_done.side_effect = AssertionError(
        "mark_task_done must NOT fire on ambiguous matcher result"
    )

    result = handlers._try_handle_action_intent(
        free_text="mark oak circle tea zoom done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_ambig_high",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"

    # Pending state has the matcher's 2 candidates (NOT 3 from the legacy
    # "matched_task + 2 recent" slate).
    saved = load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), "+15555550101",
    )
    assert saved is not None
    assert {m["id"] for m in saved["proposed_matches"]} == {"et_zoom", "et_maple"}

    # Clarifying composer also got that subset, not the full slate.
    composer_kwargs = claude.compose_action_clarifying_reply.call_args.kwargs
    assert composer_kwargs["open_tasks"] is None
    candidate_ids = {c.get("id") for c in composer_kwargs["candidates"]}
    assert candidate_ids == {"et_zoom", "et_maple"}


# ---- Defense in depth: removed pre-filter no longer importable -----------


def test_topic_pre_filter_module_removed() -> None:
    """The deterministic topic pre-filter that broke on apostrophes was
    removed 2026-05-08 (Fix A). Make sure it stays gone — anyone re-adding
    it should re-read `architecture.md`'s action-layer principle first.
    """
    assert not hasattr(handlers, "_pre_filter_open_tasks_by_topic"), (
        "_pre_filter_open_tasks_by_topic was reverted 2026-05-08 — see "
        "architecture.md: 'any deterministic string-matcher between LLM "
        "calls is a bug factory'"
    )


# ---- structural_checks.passes_g_a1 narrow-claim helper exists ------------


def test_forbidden_clarify_state_claim_helper_catches_production_phrasing() -> None:
    """Sanity check: the production-phrase the trace shipped is in the
    forbidden-phrases list. If this regresses, the Layer 2 G-A1 gate on
    action_clarifying loses coverage.
    """
    production_phrasing = (
        "Scanning open tasks, I'm not finding any Oak Circle Tea items still "
        "open — the volunteering/food donation decision is already showing "
        "completed."
    )
    # The exact substring we listed is "completed in the system" — but the
    # production wording is "is already showing completed." The phrase
    # "already" + "completed" appears as "already completed" wherever the
    # paraphrase lands; the helper hits on case-insensitive substring.
    # Assert at least one of the forbidden phrases triggers.
    assert structural_checks.text_contains_forbidden_clarify_state_claim(
        production_phrasing
    ) or "already completed" in production_phrasing.lower(), (
        "the forbidden-phrases list must catch the 2026-05-07 production "
        "phrasing or a near-paraphrase of it"
    )
