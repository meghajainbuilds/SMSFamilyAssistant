"""coordination intent classifier — LLM compose entry point.

Decides whether `free_text` is a coordination request — i.e., the requester
is asking Kavi to engage the OTHER household member.

Called from the action-intent layer in handlers AFTER the action-intent
classifier returns has_action=false (i.e., it's NOT a single-task action
like mark_done or create). This second pass catches the coordination shape
("ask Max if...") before the inbound falls to the conversational composer.

Returns:
  {
    "is_coordination": bool,
    "addressee_name": str | None,
    "addressee_handle": str | None,
    "coordination_ask": str | None,
    "confidence": "high" | "medium" | "low",
    "_usage": {...},
  }

Failing-safe: API or parse error returns is_coordination=false so the
caller falls through to the conversational reply. Same bias as the
action-intent classifier — a missed coordination is recoverable in one
round trip; a fabricated coordination pings the addressee unnecessarily
and erodes trust.

Phase 4 (2026-06-02): physically moved out of
kavi_runtime/claude_client.ClaudeClient.classify_coordination_intent.
The thin proxy on `ClaudeClient` delegates here. Function body is canonical.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient, _est_input_tokens

logger = logging.getLogger(__name__)


def classify_coordination_intent(
    client: ClaudeClient,
    free_text: str,
    requester_handle: str,
    household_members: list[dict[str, Any]],
) -> dict[str, Any]:
    """Body of the coordination intent classifier. See module docstring."""
    skill = client._skill("coordination_intent_classifier")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload = {
        "free_text": free_text,
        "requester_handle": requester_handle,
        "household_members": household_members,
    }
    user_msg = (
        "Classify this iMessage per the skill procedure. Return ONLY a single JSON object "
        "with fields {is_coordination, addressee_name, addressee_handle, coordination_ask, confidence}. "
        "No prose around it.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    fallback = {
        "is_coordination": False,
        "addressee_name": None,
        "addressee_handle": None,
        "coordination_ask": None,
        "confidence": "low",
        "_usage": None,
    }

    model = client._model_for_call_type("classify_coordination_intent")
    started = client._log_call_start("classify_coordination_intent", model,
                                    _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("classify_coordination_intent", started, e)
        logger.warning("classify_coordination_intent API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("classify_coordination_intent", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("classify_coordination_intent usage: %s", usage)

    parsed = client._extract_json(text)
    if parsed is None or not isinstance(parsed, dict):
        logger.warning("classify_coordination_intent returned non-JSON: %s", text[:300])
        return {**fallback, "_usage": usage, "_error": "parse_error"}

    is_coord = parsed.get("is_coordination")
    if not isinstance(is_coord, bool):
        return {**fallback, "_usage": usage, "_error": "bad_is_coordination"}
    confidence = parsed.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    addressee_name = parsed.get("addressee_name")
    addressee_handle = parsed.get("addressee_handle")
    coordination_ask = parsed.get("coordination_ask")
    if addressee_name is not None and not isinstance(addressee_name, str):
        addressee_name = None
    if addressee_handle is not None and not isinstance(addressee_handle, str):
        addressee_handle = None
    if coordination_ask is not None and not isinstance(coordination_ask, str):
        coordination_ask = None

    return {
        "is_coordination": is_coord,
        "addressee_name": addressee_name,
        "addressee_handle": addressee_handle,
        "coordination_ask": coordination_ask,
        "confidence": confidence,
        "_usage": usage,
    }


__all__ = ["classify_coordination_intent"]
