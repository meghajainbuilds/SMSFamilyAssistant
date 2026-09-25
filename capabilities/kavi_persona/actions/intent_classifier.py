"""Capability function: classify_action_intent.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.classify_action_intent(...)` keep working.

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


def classify_action_intent(
    client,
    free_text: str,
    recent_kavi_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Stage 1 action layer: classify a free-text iMessage as an imperative
    action request (mark_done / create / update / cancel) or no-action.

    Called AFTER classify_correction returns not_correction and BEFORE the
    conversational-reply path. Stage 1 only acts on `mark_done` with
    `confidence=high`; everything else falls through to conversation.

    Returns (per skill spec):
      {"has_action": bool,
       "action_type": "mark_done" | "create" | "update" | "cancel" | None,
       "target_text": str | None,
       "confidence": "high" | "medium" | "low",
       "_usage": {...}}

    On parse error or invalid shape: returns has_action=false so the caller
    falls through to the conversational reply. Failing-safe: a missed action
    is recoverable; a fabricated action is not.
    """
    skill = client._skill("action_intent_classifier")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload = {
        "free_text": free_text,
        "recent_kavi_messages": recent_kavi_messages or [],
    }
    user_msg = (
        "Classify this iMessage per the skill procedure. Return ONLY a single JSON object "
        "with fields {has_action, action_type, target_text, confidence}. No prose around it.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    model = client._model_for_call_type("classify_action_intent")
    started = client._log_call_start("classify_action_intent", model,
                                    _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("classify_action_intent", started, e)
        logger.warning("classify_action_intent API call failed: %s", e)
        return {
            "has_action": False,
            "action_type": None,
            "target_text": None,
            "confidence": "low",
            "_usage": None,
            "_error": f"api_error: {e}",
        }

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("classify_action_intent", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("classify_action_intent usage: %s", usage)

    parsed = client._extract_json(text)
    fallback = {
        "has_action": False,
        "action_type": None,
        "target_text": None,
        "confidence": "low",
        "_usage": usage,
    }
    if parsed is None or not isinstance(parsed, dict):
        logger.warning("classify_action_intent returned non-JSON: %s", text[:300])
        return {**fallback, "_error": "parse_error"}
    if not isinstance(parsed.get("has_action"), bool):
        logger.warning("classify_action_intent bad has_action: %r", parsed.get("has_action"))
        return {**fallback, "_error": "bad_has_action"}
    action_type = parsed.get("action_type")
    if action_type not in {"mark_done", "create", "update", "cancel", None}:
        logger.warning("classify_action_intent bad action_type: %r", action_type)
        return {**fallback, "_error": "bad_action_type"}
    confidence = parsed.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    target_text = parsed.get("target_text")
    if target_text is not None and not isinstance(target_text, str):
        target_text = None
    return {
        "has_action": parsed["has_action"],
        "action_type": action_type,
        "target_text": target_text,
        "confidence": confidence,
        "_usage": usage,
    }
