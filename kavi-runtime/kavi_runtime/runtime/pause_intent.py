"""Capability function: classify_pause_intent.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.classify_pause_intent(...)` keep working.

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


def classify_pause_intent(client, free_text: str, pause_context: dict[str, Any]) -> dict[str, Any]:
    """While the runtime is paused, decide whether Megha's iMessage is asking to
    resume the runtime, asking about something else (correction / Q&A / chat), or
    ambiguous.

    Pause-context fields the classifier sees:
      - paused_since: ISO timestamp
      - paused_reason: e.g. "spend_cap_exceeded"
      - paused_spend_usd: float (if spend trip)
      - cap_usd: configured monthly cap

    Returns:
      {"intent": "resume" | "not_resume" | "ambiguous", "reason": "..."}

    On parse error, defaults to "not_resume" so the message routes to the existing
    correction classifier — failing safe (we don't accidentally clear the pause).
    """
    skill = client._skill("pause_intent_classifier")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload = {
        "free_text": free_text,
        "pause_context": pause_context,
    }
    user_msg = (
        "Classify this iMessage per the skill procedure. Return ONLY a single JSON object "
        "with fields {intent, reason}. No prose around it.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    model = client._model_for_call_type("classify_pause_intent")
    started = client._log_call_start("classify_pause_intent", model,
                                    _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("classify_pause_intent", started, e)
        raise
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("classify_pause_intent", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("classify_pause_intent usage: %s", usage)
    parsed = client._extract_json(text)
    if parsed is None or "intent" not in parsed:
        logger.warning("classify_pause_intent returned non-JSON: %s", text[:200])
        return {"intent": "not_resume", "reason": "parse_error", "_usage": usage}
    if parsed["intent"] not in {"resume", "not_resume", "ambiguous"}:
        logger.warning("classify_pause_intent bad intent value: %r", parsed.get("intent"))
        return {"intent": "not_resume", "reason": "bad_intent_value", "_usage": usage}
    parsed["_usage"] = usage
    return parsed
