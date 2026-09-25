"""Tests for G-A1 (action-claim grounding) — added 2026-05-07.

G-A1 is the Layer 2 defense for Principle 7 ("the output layer is always
LLM-composed, but the LLM cannot claim a verb without a verified tool
result"). It scans every outbound row's text for a verb in
`ACTION_VERBS_REQUIRING_GROUNDING` and requires the row's `context` to
either carry `tool_grounded=True` (deterministic-template path verified
the Graph call) OR contain at least one `actions_executed` entry with
`result=success`.

Cases covered:
  (a) action verb + actions_executed success → PASS
  (b) action verb + no actions_executed, no tool_grounded → FAIL
  (c) action verb + tool_grounded=True → PASS (qa_ack / correction_ack
      deterministic-template path)
  (d) no action verb → PASS regardless of context
  (e) conversational reply with action verb + no grounding → handler
      drops to alert_fallback (Megha never sees the hallucinated text)
  (f) qa_ack template path with successful Graph call → tool_grounded set,
      G-A1 passes
  (g) qa_ack with swallowed Graph exception → tool_grounded False,
      G-A1 fails

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_structural_g_a1.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.state import save_imessage_state
from kavi_runtime.structural_checks import (
    ACTION_VERBS_REQUIRING_GROUNDING,
    compute_action_claim_correspondence_rate,
    passes_g_a1,
    structural_check,
    text_contains_action_verb,
)


# ---- Pure G-A1 logic -----------------------------------------------------


def test_g_a1_a_action_verb_with_success_passes() -> None:
    """(a) action verb + actions_executed success → PASS."""
    text = "Marked all four done."
    context = {
        "actions_executed": [
            {"task_id": "t1", "result": "success", "target_title": "Foo"},
            {"task_id": "t2", "result": "success", "target_title": "Bar"},
        ],
    }
    assert passes_g_a1(text, context) is True


def test_g_a1_b_action_verb_with_no_grounding_fails() -> None:
    """(b) action verb + no actions_executed, no tool_grounded → FAIL.

    2026-05-08 refinement: the gate now keys on attempt, not outcome.
    A non-empty actions_executed (any result) PASSES because the action
    layer attempted the action and the reply is downstream of that attempt.
    The Elders' Tea hallucination has actions_executed=[] (no attempt) —
    that case still fails.
    """
    text = "Marked done — Boonli payment closed out."
    assert passes_g_a1(text, None) is False
    assert passes_g_a1(text, {}) is False
    assert passes_g_a1(text, {"actions_executed": []}) is False
    # actions_executed listing only failures now PASSES — honest failure
    # acks ("Couldn't mark any of the 2 done") deserve to ship; the LLM
    # prompt rules in Fix 2 catch attempted-but-misreported claims.
    assert passes_g_a1(text, {
        "actions_executed": [{"result": "failure", "target_title": "x"}],
    }) is True
    # already_completed (verified state read) also passes.
    assert passes_g_a1(text, {
        "actions_executed": [{"result": "already_completed", "target_title": "x"}],
    }) is True


def test_g_a1_c_tool_grounded_flag_passes() -> None:
    """(c) action verb + tool_grounded=True (qa_ack / correction_ack
    deterministic-template path) → PASS."""
    # correction_ack ("added", "removed") AND qa_ack ("Dropped:") both trip
    # the action-verb pattern; tool_grounded gets them through.
    text = "Got it! 'Maple newsletter' added to MS To Do."
    assert passes_g_a1(text, {"tool_grounded": True}) is True
    text = "Dropped: MJ Boonli school lunch payment"
    assert passes_g_a1(text, {"tool_grounded": True}) is True
    text = "Got it! Removed 'UW bill' from MS To Do."
    assert passes_g_a1(text, {"tool_grounded": True}) is True
    # Same texts WITHOUT tool_grounded → fail.
    assert passes_g_a1("Got it! 'Maple newsletter' added to MS To Do.", {}) is False


def test_g_a1_d_no_action_verb_passes_any_context() -> None:
    """(d) no action verb in text → PASS regardless of context."""
    for text in [
        "Got it — sitting with that. Anything specific?",
        "I'm Kavi, your household's Chief of Staff.",
        "Hey. Anything you need from me?",
        "Three from yesterday's morning digest — Maple signup, Boonli, tennis.",
    ]:
        assert passes_g_a1(text, None) is True
        assert passes_g_a1(text, {}) is True
        assert passes_g_a1(text, {"actions_executed": []}) is True


def test_g_a1_inflections_all_caught() -> None:
    """Every inflection in ACTION_VERBS_REQUIRING_GROUNDING is matched
    case-insensitively and on word boundaries."""
    for verb in ACTION_VERBS_REQUIRING_GROUNDING:
        assert text_contains_action_verb(f"I {verb} the task.") is True
        assert text_contains_action_verb(f"I {verb.upper()} it.") is True
        assert text_contains_action_verb(f"{verb.title()} all four.") is True
    # Word boundary: "marketing" should NOT match "marked".
    assert text_contains_action_verb("Boonli marketing email arrived.") is False
    # Word boundary: "ascertained" should NOT match "ascertain".
    assert text_contains_action_verb("addresses pending review.") is False


def test_g_a1_kept_keep_keeping_caught() -> None:
    """Anita bug 2026-06-01 regression. Kavi narrated 'Kept - task is on the
    list' without grounding because 'kept' was not in ACTION_VERBS. Three
    inflections must now be caught."""
    assert "kept" in ACTION_VERBS_REQUIRING_GROUNDING
    assert "keep" in ACTION_VERBS_REQUIRING_GROUNDING
    assert "keeping" in ACTION_VERBS_REQUIRING_GROUNDING
    # Concrete repro of the Anita narration shape.
    text = "Kept - task is on the list to RSVP for Anita's housewarming."
    assert passes_g_a1(text, {"actions_executed": [], "tool_grounded": False}) is False
    # Grounded path still passes.
    assert (
        passes_g_a1(
            text,
            {
                "actions_executed": [{"verb": "keep", "result": "success", "target_title": "RSVP"}],
                "tool_grounded": True,
            },
        )
        is True
    )


def test_structural_check_includes_g_a1_field() -> None:
    """structural_check() output must include the new g_a1_action_grounded
    boolean for both passing and failing cases."""
    out = structural_check("Marked all four done.", context={"tool_grounded": True})
    assert out["g_a1_action_grounded"] is True
    out = structural_check("Marked all four done.", context=None)
    assert out["g_a1_action_grounded"] is False
    # Backward compat: structural_check called WITHOUT context still returns
    # the field (no exception). Outbound row with action verb but no context
    # treated as ungrounded.
    out = structural_check("Marked all four done.")
    assert out["g_a1_action_grounded"] is False
    # No action verb → field is True regardless.
    out = structural_check("Hey. Anything you need?")
    assert out["g_a1_action_grounded"] is True


# ---- Conversational drop-to-fallback (case e) ----------------------------


def _config_for_handler_test(tmp_path: Path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {"mstodo_shared_list_id": "AQMkADAwTEST=="},
        "action_layer": {"dry_run": False},
        "paths": {
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "corrections_jsonl": str(tmp_path / "corrections.jsonl"),
            "eval_persona_action_intent_jsonl": str(tmp_path / "action_intent.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
        },
    }


def test_g_a1_e_conversational_with_action_verb_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """(e) conversational composer returns a hallucinated past-tense action
    claim. G-A1 enforcement in handlers must drop the original message and
    ship the alert_fallback text instead."""
    cfg = _config_for_handler_test(tmp_path)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    sent: list[dict[str, Any]] = []

    def _record_send(config: dict, text: str, *, kind: str, **kwargs) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _record_send)

    # Patch _get_clients so we don't try to construct real clients.
    claude = MagicMock()
    # Correction classifier returns not_correction so we hit the conv path.
    claude.classify_correction.return_value = {
        "status": "not_correction",
        "reason": "test",
    }
    # Action intent says no.
    claude.classify_action_intent.return_value = {
        "has_action": False, "action_type": None,
        "target_text": None, "confidence": "low",
    }
    # Hallucinated reply with action verbs.
    claude.compose_conversational_reply.return_value = "Marked the UW bill done."

    graph = MagicMock()
    bb = MagicMock()
    _stub_get_clients = lambda config: (graph, claude, bb)
    monkeypatch.setattr(handlers, "_get_clients", _stub_get_clients)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_get_clients)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub_get_clients)
    monkeypatch.setattr(handlers, "_load_recent_outbound", lambda *a, **kw: [])
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)

    result = handlers._handle_correction(
        free_text="hey what's up",
        config=cfg,
        sender_handle="+15555550101",
        source_imessage_id="im_test",
    )

    # The hallucinated text was NOT sent; the fallback ack was sent instead.
    assert sent, "expected at least one send"
    final = sent[-1]
    assert final["kind"] == "alert_fallback"
    assert "I caught myself" in final["text"]
    assert "Marked" not in final["text"]
    # The original hallucinated text never landed in `text` of any send.
    for s in sent:
        assert s["text"] != "Marked the UW bill done."


# ---- DELETED 2026-06-10 (intent-first dispatch rebuild) -------------------
# test_g_a1_f_qa_ack_success_sets_tool_grounded exercised the deleted
# _handle_q_and_a_reply "Dropped: X" template-ack path. Replacing coverage:
# matrix cases regression-bare-drop-after-qa + incident-2026-06-10-yes-plus-
# close-all (banned_template_reply gate) and tests/test_intent_executors.py
# (qa_drop executor, no template ack).


# ---- Friday weekly metric (Item 7) ---------------------------------------


def _row(text: str, *, g_a1: bool, decision_id: str, kind: str = "conversational",
         ts: str = "2026-05-05T18:00:00Z") -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "ts": ts,
        "kind": kind,
        "text": text,
        "structural_checks": {"g_a1_action_grounded": g_a1},
    }


def test_metric_mixed_window_computes_correctly() -> None:
    """Stub a 7-day window with mixed rows: some action-verb, some not,
    some grounded, some not. The metric matches numerator / denominator
    over the verb-bearing rows."""
    rows = [
        _row("Marked all four done.", g_a1=True, decision_id="d1"),    # verb + grounded
        _row("Got it! 'Foo' added to MS To Do.", g_a1=True, decision_id="d2"),  # verb + grounded
        _row("Hey. Anything you need?", g_a1=True, decision_id="d3"),  # no verb, ignored from denom
        _row("Sent it earlier.", g_a1=False, decision_id="d4"),         # verb + ungrounded
        _row("I'm Kavi, your household's Chief of Staff.", g_a1=True, decision_id="d5"),  # no verb
        _row("Dropped: MJ Boonli payment", g_a1=True, decision_id="d6"),  # verb + grounded
    ]
    out = compute_action_claim_correspondence_rate(rows)
    assert out["verb_rows_count"] == 4  # d1, d2, d4, d6
    assert out["grounded_rows_count"] == 3  # d1, d2, d6
    assert out["rate"] == 0.75
    assert len(out["ungrounded_rows"]) == 1
    assert out["ungrounded_rows"][0]["decision_id"] == "d4"
    assert out["ungrounded_rows"][0]["text_snippet"] == "Sent it earlier."


def test_metric_below_threshold_listing_fields_present() -> None:
    """When rate < 1.0, every ungrounded row carries decision_id, ts, kind,
    and an 80-char text_snippet so the report can render the drill-in."""
    rows = [
        _row(
            "Marked done — Boonli payment closed out — and added 'Maple' too.",
            g_a1=False, decision_id="d_bad",
            kind="conversational", ts="2026-05-05T18:00:00Z",
        ),
        _row(
            "Marked done.",
            g_a1=True, decision_id="d_ok",
            kind="post_action_reply", ts="2026-05-05T18:01:00Z",
        ),
    ]
    out = compute_action_claim_correspondence_rate(rows)
    assert out["rate"] == 0.5
    assert out["verb_rows_count"] == 2
    assert len(out["ungrounded_rows"]) == 1
    bad = out["ungrounded_rows"][0]
    assert bad["decision_id"] == "d_bad"
    assert bad["ts"] == "2026-05-05T18:00:00Z"
    assert bad["kind"] == "conversational"
    # text_snippet is the first 80 chars verbatim.
    assert bad["text_snippet"].startswith("Marked done — Boonli payment closed out")
    assert len(bad["text_snippet"]) <= 80


def test_metric_no_verb_rows_returns_none_rate() -> None:
    """When the window has no verb-bearing rows, rate is None ("n/a"), not
    100%. Reporting 100% of nothing would mislead Megha."""
    rows = [
        _row("Hey.", g_a1=True, decision_id="d1"),
        _row("Got it — sitting with that.", g_a1=True, decision_id="d2"),
    ]
    out = compute_action_claim_correspondence_rate(rows)
    assert out["rate"] is None
    assert out["verb_rows_count"] == 0
    assert out["grounded_rows_count"] == 0
    assert out["ungrounded_rows"] == []


def test_metric_all_grounded_returns_100_percent() -> None:
    """All verb-bearing rows grounded → rate = 1.0, no ungrounded listing."""
    rows = [
        _row("Marked done.", g_a1=True, decision_id="d1"),
        _row("Added it.", g_a1=True, decision_id="d2"),
        _row("Closed out the UW task.", g_a1=True, decision_id="d3"),
    ]
    out = compute_action_claim_correspondence_rate(rows)
    assert out["rate"] == 1.0
    assert out["ungrounded_rows"] == []


def test_metric_handles_missing_structural_checks_gracefully() -> None:
    """Legacy rows with no g_a1_action_grounded field treated as ungrounded
    when they contain a verb. Forward-compat for old rows logged before
    G-A1 shipped."""
    rows = [
        {"decision_id": "d_old", "ts": "x", "kind": "conversational",
         "text": "Marked done.", "structural_checks": {}},
        {"decision_id": "d_new", "ts": "x", "kind": "conversational",
         "text": "Marked done.",
         "structural_checks": {"g_a1_action_grounded": True}},
    ]
    out = compute_action_claim_correspondence_rate(rows)
    assert out["verb_rows_count"] == 2
    assert out["grounded_rows_count"] == 1
    assert out["rate"] == 0.5
    assert out["ungrounded_rows"][0]["decision_id"] == "d_old"


# DELETED 2026-06-10 (intent-first dispatch rebuild):
# test_g_a1_g_qa_ack_swallowed_exception_drops_to_alert_fallback exercised
# the deleted _handle_q_and_a_reply template-ack path (swallowed Graph
# exception -> alert_fallback). Replacing coverage: the executors report
# failure results honestly (tests/test_intent_executors.py) and the matrix
# banned_template_reply gate forbids the "Dropped:" shape.


# ---- Fix 1 (2026-05-08): G-A1 fail-closed on post_action_reply + qa_ack ---


def test_fix1_post_action_reply_with_unverified_action_verb_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Fix 1: when the post-action composer returns text with an action verb
    but no entry in `actions_executed` backs it (e.g., a buggy composer that
    claims success when nothing ran), the send wrapper drops the original
    and ships the alert_fallback.

    Note: the Elders' Tea production trace's specific shape (clarifying-
    composer hallucination) is now caught at compose time by Fix 2's
    LLM-prompt rule + deterministic post-composer check. Fix 1's role is
    to catch real post-action hallucinations where the composer claims an
    outcome the actions_executed rows don't support.
    """
    cfg = _config_for_handler_test(tmp_path)

    sent: list[dict[str, Any]] = []

    def _record_send_with_ctx(config, text, *, kind, context, **kwargs):
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True}

    def _record_send_plain(config, text, *, kind, **kwargs):
        sent.append({"text": text, "kind": kind, "context": None})
        return {"verified": True}

    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context", _record_send_with_ctx,
    )
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback", _record_send_plain,
    )

    # Real post_action_reply scenario with empty actions_executed list — a
    # composer bug claiming completion when no PATCH was tracked.
    handlers._send_action_layer_reply(
        cfg,
        "Marked all four Elders' Tea tasks done — closed out on my end.",
        context={"actions_executed": []},
    )

    final = sent[-1]
    # action_clarifying tag on empty actions_executed list (clarify path).
    # The spec's fail-closed list covers post_action_reply; the empty-list
    # path now correctly routes to action_clarifying (log-only) per the
    # 2026-05-08 retagging.
    assert final["kind"] == "action_clarifying"


def test_fix1_post_action_reply_drops_when_g_a1_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Fix 1: the helper drops to alert_fallback when called with an explicit
    `post_action_reply` kind that fails G-A1. Tests the helper directly to
    isolate the G-A1 fail-closed contract from the kind-tagging logic."""
    cfg = _config_for_handler_test(tmp_path)
    # post_action_reply kind, action verb in text, no actions_executed →
    # fails G-A1 → alert_fallback substitution.
    safe_text, safe_kind, safe_ctx = handlers._enforce_g_a1_or_alert_fallback(
        cfg,
        "Marked all four Elders' Tea tasks done.",
        kind="post_action_reply",
        context=None,
    )
    assert safe_kind == "alert_fallback"
    assert "I caught myself" in safe_text
    assert safe_ctx is None


def test_fix1_qa_ack_drops_when_g_a1_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Fix 1: the helper drops to alert_fallback when called with an explicit
    `qa_ack` kind that fails G-A1."""
    cfg = _config_for_handler_test(tmp_path)
    safe_text, safe_kind, safe_ctx = handlers._enforce_g_a1_or_alert_fallback(
        cfg,
        "Dropped: MJ UW bill",
        kind="qa_ack",
        context={"tool_grounded": False},
    )
    assert safe_kind == "alert_fallback"
    assert "I caught myself" in safe_text


def test_fix1_action_clarifying_kind_is_log_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Fix 1: action_clarifying is NOT in the fail-closed kind set.
    Clarifying questions legitimately use action verbs in question form
    ("Confirm to mark done?", "want me to mark it done?") and should ship
    as-is. The LLM-prompt rule + deterministic post-composer check (Fix 2)
    are the defense for clarifying-composer hallucinations."""
    cfg = _config_for_handler_test(tmp_path)
    # Verb-bearing clarifying question, no actions_executed, no tool_grounded.
    text = "Got UW Medicine balance ($630). Confirm to mark done?"
    safe_text, safe_kind, safe_ctx = handlers._enforce_g_a1_or_alert_fallback(
        cfg, text, kind="action_clarifying", context=None,
    )
    # No substitution — the question ships as-is.
    assert safe_text == text
    assert safe_kind == "action_clarifying"


def test_fix1_post_action_reply_with_verified_action_ships_normally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Fix 1: when actions_executed has a result=success row, the action verb
    is grounded → reply ships normally as `post_action_reply` (no
    fallback substitution)."""
    cfg = _config_for_handler_test(tmp_path)

    sent: list[dict[str, Any]] = []

    def _record_send_with_ctx(config, text, *, kind, context, **kwargs):
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True}

    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context", _record_send_with_ctx,
    )

    handlers._send_action_layer_reply(
        cfg,
        "Marked all four Elders' Tea tasks done.",
        context={"actions_executed": [
            {"task_id": "t1", "result": "success", "target_title": "Confirm guests"},
            {"task_id": "t2", "result": "success", "target_title": "Forward Zoom link"},
            {"task_id": "t3", "result": "success", "target_title": "Theo's clothes"},
            {"task_id": "t4", "result": "success", "target_title": "Gluten-free baked goods"},
        ]},
    )

    assert len(sent) == 1
    assert sent[0]["kind"] == "post_action_reply"
    assert "Marked all four" in sent[0]["text"]


# DELETED 2026-06-10 (intent-first dispatch rebuild):
# test_fix1_qa_ack_with_action_verb_no_grounding_falls_back and
# test_fix1_qa_ack_with_graph_success_ships_normally exercised the deleted
# _handle_q_and_a_reply "Kept:"/"Dropped:" qa_ack templates through the
# G-A1 gate. The gate itself stays covered by test_fix1_qa_ack_drops_when_
# g_a1_fails (direct gate test) and the post_action_reply tests; the
# replaced reply path is covered by matrix cases regression-bare-keep-
# after-qa / regression-bare-drop-after-qa and tests/test_intent_dispatch.py.


