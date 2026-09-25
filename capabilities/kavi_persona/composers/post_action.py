"""Capability function: compose_post_action_reply.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.compose_post_action_reply(...)` keep working.

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


def compose_post_action_reply(
    client,
    action_type: str,
    target_title: str,
    result: str,
    failure_reason: str | None = None,
) -> str | None:
    """Stage 1 action layer: compose Kavi's iMessage acknowledging that an
    MS To Do action has already executed (or failed). Past-tense framing only;
    do NOT use the "On it" pre-action pattern that fails AV.

    Returns the message string (≤180 char hard cap) or None on error / empty
    / over-length so the caller can fall back to a deterministic template.
    """
    system = client._build_system_prompt("post_action_reply_composer")
    input_payload = {
        "action_type": action_type,
        "target_title": target_title,
        "result": result,
        "failure_reason": failure_reason,
    }
    user_msg = (
        "Compose ONE post-action iMessage to Megha based on the input. Output JSON: "
        "{\"message\": \"<your message, <=120 chars>\"}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )
    model = client._model_for_call_type("compose_post_action_reply")
    started = client._log_call_start(
        "compose_post_action_reply", model,
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
            logger.info("compose_post_action_reply usage: %s", usage)
        except Exception:
            usage = None
        text = resp.content[0].text if resp.content else ""
        client._log_call_done("compose_post_action_reply", model, started, usage,
                             input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        if not parsed or "message" not in parsed:
            logger.warning("compose_post_action_reply: bad output %r", text[:200])
            return None
        msg = (parsed.get("message") or "").strip()
        if not msg:
            logger.warning("compose_post_action_reply: empty message")
            return None
        if len(msg) > 180:
            logger.warning("compose_post_action_reply: over-length %d chars; rejecting", len(msg))
            return None
        return msg
    except Exception as e:
        client._log_call_failed("compose_post_action_reply", started, e)
        logger.exception("compose_post_action_reply failed: %s", e)
        return None
