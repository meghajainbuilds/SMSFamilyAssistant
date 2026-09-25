"""Capability function: compose_action_clarifying_reply.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.compose_action_clarifying_reply(...)` keep working.

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

from kavi_runtime.claude_client import _safe_clarify_fallback_question

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list, user_msg: str) -> int:
    """Local helper duplicated from claude_client to avoid circular import."""
    total = sum(len(b.get("text", "")) for b in system)
    total += len(user_msg)
    return int(total / 4)


def compose_action_clarifying_reply(
    client,
    free_text: str,
    action_type: str,
    target_text: str | None,
    open_tasks: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    recent_outbound: list[dict[str, Any]] | None = None,
) -> str | None:
    """LLM-composed clarifying reply for the action layer.

    Replaces the deterministic `_compose_clarifying_reply` template that
    used to fill an empty `target_text` field with the literal word
    "that" — leaking the placeholder into Megha's iMessage. Per
    `kavi-persona.md` Principle 7 (the output layer is always LLM-
    composed), every user-facing reply path goes through the LLM with
    deterministic steps as input only.

    Returns the reply string (capped at 240 chars to leave room for
    multi-item acknowledgment, but voice rules prefer ≤120) or None on
    LLM failure / empty / over-length so the caller can use the cold
    fallback. Failing-safe: cold fallback only fires when the LLM is
    unreachable.
    """
    skill = client._skill("action_clarifying_reply_composer")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    # Trim each task to the fields the composer uses; cap at 30 to keep
    # input bounded even if the caller passes the full list.
    trimmed_tasks: list[dict[str, Any]] = []
    for t in (open_tasks or [])[:30]:
        if not isinstance(t, dict):
            continue
        title = t.get("title") or ""
        if not title:
            continue
        trimmed_tasks.append({
            "id": t.get("id"),
            "title": title,
            "status": t.get("status"),
        })

    trimmed_candidates: list[dict[str, Any]] = []
    for c in (candidates or [])[:5]:
        if not isinstance(c, dict):
            continue
        trimmed_candidates.append({
            "id": c.get("id"),
            "title": c.get("title") or "",
            "match_reasoning": (c.get("match_reasoning") or c.get("reasoning") or "")[:120],
        })

    recent = []
    for r in (recent_outbound or [])[-5:]:
        recent.append({
            "ts": r.get("ts"),
            "kind": r.get("kind"),
            "text": (r.get("text") or "")[:200],
        })

    input_payload = {
        "free_text": free_text,
        "action_type": action_type,
        "target_text": target_text or "",
        "open_tasks": trimmed_tasks,
        "candidates": trimmed_candidates,
        "recent_outbound": recent,
    }
    user_msg = (
        "Compose ONE clarifying iMessage to Megha based on the input. "
        "Output JSON: {\"message\": \"<your reply>\"}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )
    model = client._model_for_call_type("compose_action_clarifying_reply")
    started = client._log_call_start(
        "compose_action_clarifying_reply", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("compose_action_clarifying_reply", started, e)
        logger.exception("compose_action_clarifying_reply failed: %s", e)
        return None

    try:
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
        logger.info("compose_action_clarifying_reply usage: %s", usage)
    except Exception:
        usage = None
    text = resp.content[0].text if resp.content else ""
    client._log_call_done("compose_action_clarifying_reply", model, started, usage,
                         input_text=user_msg, output_text=text)
    parsed = client._extract_json(text)
    if not parsed or "message" not in parsed:
        logger.warning("compose_action_clarifying_reply: bad output %r", text[:200])
        return None
    msg = (parsed.get("message") or "").strip()
    if not msg:
        logger.warning("compose_action_clarifying_reply: empty message")
        return None
    # Generous upper bound to allow multi-item acknowledgment; voice
    # rules still prefer ≤120 and the skill prompt enforces that.
    if len(msg) > 240:
        logger.warning(
            "compose_action_clarifying_reply: over-length %d chars; rejecting",
            len(msg),
        )
        return None
    # Fix 2 (2026-05-08): deterministic post-composer check for forbidden
    # state-claim phrases. The clarifying composer has NOT executed any
    # tool — any past-tense state claim ("already done", "marked done",
    # "nothing left open", etc.) is a hallucination. Belt-and-suspenders
    # with the LLM-prompt rule in the skill: if the model still produces
    # a banned phrase, deterministically rewrite to a safe fallback
    # question rather than ship the lie. The 2026-05-07 Elders' Tea
    # production trace ("All Elders' Tea tasks are already marked done")
    # is exactly the case this check guards.
    from kavi_runtime.structural_checks import (
        text_contains_forbidden_clarify_state_claim,
    )
    if text_contains_forbidden_clarify_state_claim(msg):
        logger.warning(
            "compose_action_clarifying_reply: forbidden state-claim "
            "phrase in output; rewriting to safe fallback. Original=%r",
            msg[:240],
        )
        msg = _safe_clarify_fallback_question(
            free_text=free_text,
            action_type=action_type,
            target_text=target_text,
            open_tasks=trimmed_tasks,
        )
    return msg
