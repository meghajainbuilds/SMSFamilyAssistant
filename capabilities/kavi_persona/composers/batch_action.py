"""Capability function: compose_batch_action_reply.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.compose_batch_action_reply(...)` keep working.

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


def compose_batch_action_reply(
    client,
    actions_executed: list[dict[str, Any]],
    *,
    needs_clarification: bool = False,
    ambiguous_reply: str | None = None,
    proposed_matches: list[dict[str, Any]] | None = None,
) -> str | None:
    """Compose Kavi's iMessage acknowledging a batch of MS To Do actions
    that have already executed (or attempted), OR composing a clarifying
    question when the resolver returned execute-with-empty-ids.

    Added 2026-05-07 (action-hallucination fix F2). The pending-clarification
    path used to ship a past-tense reply BEFORE any PATCH ran, then log
    per-task failures silently. Now the runtime executes the PATCHes,
    builds `actions_executed[]` with verified `{id, title, result,
    failure_reason?}`, and calls this composer with the verified results.
    Per Principle 7: every action verb in the reply traces to a verified
    tool result.

    Args:
      actions_executed: list of {task_id, action_type, target_title,
                        result: "success"|"failure", failure_reason?}.
                        Empty when needs_clarification=True.
      needs_clarification: True when the resolver said execute with empty
                           ids (Megha's reply was ambiguous). The composer
                           asks ONE specific clarifying question instead
                           of a past-tense ack. No PATCH ran.
      ambiguous_reply: Megha's actual reply text, only when
                       needs_clarification=True.
      proposed_matches: subset of pending["proposed_matches"] (id, title)
                        for the clarifying question to reference. Only
                        populated when needs_clarification=True.

    Returns the message string (≤180 char hard cap) or None on error /
    empty / over-length so the caller can use a deterministic cold
    fallback.
    """
    system = client._build_system_prompt("post_action_reply_composer")
    input_payload: dict[str, Any] = {
        "actions_executed": actions_executed or [],
        "needs_clarification": needs_clarification,
    }
    if needs_clarification:
        if ambiguous_reply is not None:
            input_payload["ambiguous_reply"] = ambiguous_reply[:240]
        if proposed_matches:
            # Trim to {id, title} so the LLM has the minimum it needs to
            # name the subsets in the question.
            input_payload["proposed_matches"] = [
                {"id": m.get("id"), "title": (m.get("title") or "")[:120]}
                for m in proposed_matches[:10]
                if isinstance(m, dict) and m.get("id")
            ]
    user_msg = (
        "Compose ONE iMessage to Megha based on the input. Output JSON: "
        "{\"message\": \"<your reply, <=120 chars>\"}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )
    model = client._model_for_call_type("compose_batch_action_reply")
    started = client._log_call_start(
        "compose_batch_action_reply", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        try:
            usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
            logger.info("compose_batch_action_reply usage: %s", usage)
        except Exception:
            usage = None
        text = resp.content[0].text if resp.content else ""
        client._log_call_done("compose_batch_action_reply", model, started, usage,
                             input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        if not parsed or "message" not in parsed:
            logger.warning("compose_batch_action_reply: bad output %r", text[:200])
            return None
        msg = (parsed.get("message") or "").strip()
        if not msg:
            logger.warning("compose_batch_action_reply: empty message")
            return None
        if len(msg) > 240:
            logger.warning("compose_batch_action_reply: over-length %d chars; rejecting", len(msg))
            return None
        return msg
    except Exception as e:
        client._log_call_failed("compose_batch_action_reply", started, e)
        logger.exception("compose_batch_action_reply failed: %s", e)
        return None
