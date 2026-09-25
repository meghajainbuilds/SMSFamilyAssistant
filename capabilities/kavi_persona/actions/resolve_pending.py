"""Capability function: resolve_pending_action_clarification.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.resolve_pending_action_clarification(...)` keep working.

Voice/persona/structural rules live in the three canonical homes per
HomeOS three-axis rule:
- PERSONA   -> capabilities/kavi-persona.md
- STRUCTURE -> kavi_runtime/structural_checks.py
- BEHAVIOR  -> the skill file at kavi-runtime/skills/<name>.md
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kavi_runtime.claude_client import ClaudeClient

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list, user_msg: str) -> int:
    """Local helper duplicated from claude_client to avoid circular import."""
    total = sum(len(b.get("text", "")) for b in system)
    total += len(user_msg)
    return int(total / 4)


def resolve_pending_action_clarification(
    client,
    new_inbound_text: str,
    pending: dict[str, Any],
) -> dict[str, Any]:
    """LLM resolver for the 2026-05-07 pending-clarification gap.

    Last turn the action layer shipped a clarifying iMessage with proposed
    matches (e.g., "Two UW tasks: bill + $630 balance — mark both?").
    Megha just replied. This call decides what her reply MEANS in the
    context of the saved proposal — judgment only. It does NOT compose a
    user-facing reply; that runs in a separate downstream call AFTER the
    runtime executes the PATCHes and verifies which ones actually
    succeeded (Principle 7: any text Megha reads about a completed
    action must trace to a verified tool result).

    Resolutions:
      - `execute`: she confirmed (full or partial). `confirmed_match_ids`
        lists which proposed_matches IDs to act on. The runtime loops
        PATCHes, then a downstream composer writes the verified-result
        reply.
      - `ignore`: her reply doesn't address the proposal but isn't a new
        action either. No execution.
      - `fresh_intent`: pre-empts the pending one. Caller routes the new
        inbound through the regular action layer.

    Returns:
      {
        "resolution": "execute | ignore | fresh_intent",
        "confirmed_match_ids": [str, ...],     # subset of pending["proposed_matches"][*]["id"]
        "reasoning": str,
        "_usage": {...},
      }

    Note: `reply_text` was removed 2026-05-07 (action-hallucination fix
    F2). Earlier this method returned a past-tense ack composed BEFORE
    any PATCH ran; per-task PATCH failures got logged but the
    user-facing reply was already locked in. Now the runtime composes
    the reply downstream against verified `actions_executed[]`.

    Defense in depth: every returned `confirmed_match_ids` entry is
    validated against the input proposal. The LLM cannot fabricate a
    match_id that wasn't proposed; an invented id is dropped silently.
    """
    skill = client._skill("pending_clarification_resolver")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    # Trim the saved proposal so the call payload is bounded even when
    # the saved entry has accumulated extra fields.
    proposed: list[dict[str, Any]] = []
    for m in (pending.get("proposed_matches") or [])[:30]:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        proposed.append({
            "id": m.get("id"),
            "title": m.get("title") or "",
            "confidence": m.get("confidence") or "",
        })

    input_payload = {
        "new_inbound_text": new_inbound_text,
        "pending": {
            "set_at": pending.get("set_at"),
            "action_type": pending.get("action_type"),
            "original_inbound": (pending.get("original_inbound") or "")[:1000],
            "proposed_matches": proposed,
            "reply_sent": (pending.get("reply_sent") or "")[:500],
        },
    }
    user_msg = (
        "Resolve Megha's reply against the pending proposal per the skill "
        "procedure. Output JSON: "
        "{\"resolution\": \"...\", \"confirmed_match_ids\": [...], "
        "\"reasoning\": \"...\"}. Nothing else. Do NOT include a "
        "reply_text field; the user-facing reply is composed downstream "
        "after MS Graph confirms each PATCH outcome.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )

    fallback = {
        "resolution": "ignore",
        "confirmed_match_ids": [],
        "reasoning": "fallback (api or parse error)",
        "_usage": None,
    }

    model = client._model_for_call_type("resolve_pending_action_clarification")
    started = client._log_call_start(
        "resolve_pending_action_clarification", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("resolve_pending_action_clarification", started, e)
        logger.exception("resolve_pending_action_clarification API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    try:
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
        logger.info("resolve_pending_action_clarification usage: %s", usage)
    except Exception:
        usage = None
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    client._log_call_done("resolve_pending_action_clarification", model, started, usage,
                         input_text=user_msg, output_text=text)
    parsed = client._extract_json(text)
    if parsed is None or not isinstance(parsed, dict):
        logger.warning(
            "resolve_pending_action_clarification: non-JSON output %s", text[:300],
        )
        return {**fallback, "_usage": usage, "_error": "parse_error"}

    resolution = parsed.get("resolution")
    if resolution not in {"execute", "ignore", "fresh_intent"}:
        logger.warning(
            "resolve_pending_action_clarification: bad resolution %r", resolution,
        )
        return {**fallback, "_usage": usage, "_error": "bad_resolution"}

    valid_ids = {p["id"] for p in proposed if p.get("id")}
    confirmed = parsed.get("confirmed_match_ids") or []
    if not isinstance(confirmed, list):
        confirmed = []
    # Drop any id the LLM invented; it must come from the saved proposal.
    validated_ids: list[str] = []
    for cid in confirmed:
        if isinstance(cid, str) and cid in valid_ids:
            validated_ids.append(cid)
        elif isinstance(cid, str):
            logger.warning(
                "resolve_pending_action_clarification: dropped invented id=%r", cid[:24],
            )

    reasoning = parsed.get("reasoning") or ""
    if not isinstance(reasoning, str):
        reasoning = ""

    # NOTE (2026-05-07, F2): an execute resolution with no validated ids
    # is no longer coerced to ignore here. The runtime treats empty-ids
    # execute as "needs a follow-up clarifying question" and composes
    # that question downstream via the dedicated clarifying composer
    # (Principle 7: composer is always the LLM, not a templated string).

    return {
        "resolution": resolution,
        "confirmed_match_ids": validated_ids,
        "reasoning": reasoning[:400],
        "_usage": usage,
    }
