"""coordination addressee-message composer — message Kavi sends to the
OTHER household member (Max) on behalf of the requester (Megha).

Applies the attribution judgment per principle 1 of the capability spec.

Returns {"text": str | None, "char_count": int, "attribution_applied": bool,
"_usage": {...}}. Cap is 240 chars (double the persona 120 cap) because
coordination relays carry attribution + ask + options.

Phase 4 (2026-06-02): physically moved out of
kavi_runtime/claude_client.ClaudeClient.compose_coordination_addressee_message.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient, _est_input_tokens

logger = logging.getLogger(__name__)


def compose_coordination_addressee_message(
    client: ClaudeClient,
    inbound_text: str,
    requester_name: str,
    addressee_name: str,
    coordination_ask: str,
    attribution_judgment: dict[str, Any],
) -> dict[str, Any]:
    """Body of the addressee-message composer. See module docstring."""
    skill = client._skill("coordination_addressee_message_composer")
    # System prompt = skill prose (cached) + household.md so the composer
    # has roster identity. Keep this stable across sessions for cache hit.
    # Persona refusal layer is appended (2026-05-05) — this is a Kavi-voiced
    # outbound and inherits the same security boundaries as every other
    # composer call. Persona block (2026-05-27) loaded from the spec so
    # voice/identity stays in lockstep with capabilities/kavi-persona.md.
    parts = [
        f"# Skill\n\n{skill}",
        f"# Household context\n\n{client._household()}",
    ]
    persona = client._persona()
    if persona:
        parts.append(f"# Persona (canonical: capabilities/kavi-persona.md)\n\n{persona}")
    security = client._security_baseline()
    if security:
        parts.append(security)
    prefix = "\n\n".join(parts)
    block: dict[str, Any] = {"type": "text", "text": prefix}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    input_payload = {
        "inbound_text": inbound_text,
        "requester_name": requester_name,
        "addressee_name": addressee_name,
        "coordination_ask": coordination_ask,
        "attribution_judgment": attribution_judgment,
    }
    user_msg = (
        "Compose ONE iMessage to the addressee per the skill procedure. Output JSON: "
        "{\"message\": \"<your message, <=240 chars>\", \"attribution_applied\": <bool>}. "
        "Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )

    fallback = {
        "text": None,
        "char_count": 0,
        "attribution_applied": False,
        "_usage": None,
    }
    model = client._model_for_call_type("compose_coordination_addressee_message")
    started = client._log_call_start(
        "compose_coordination_addressee_message", model,
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
        client._log_call_failed("compose_coordination_addressee_message", started, e)
        logger.exception("compose_coordination_addressee_message API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("compose_coordination_addressee_message", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("compose_coordination_addressee_message usage: %s", usage)

    parsed = client._extract_json(text)
    if not parsed or "message" not in parsed:
        logger.warning("compose_coordination_addressee_message: bad output %r", text[:200])
        return {**fallback, "_usage": usage, "_error": "parse_error"}
    msg = (parsed.get("message") or "").strip()
    if msg == "REFUSED_PERSONA_CATEGORICAL":
        logger.warning("compose_coordination_addressee_message: persona refused (categorical)")
        return {**fallback, "_usage": usage, "_error": "refused_persona"}
    if not msg:
        return {**fallback, "_usage": usage, "_error": "empty_message"}
    if len(msg) > 240:
        logger.warning(
            "compose_coordination_addressee_message: over-length %d chars; truncating",
            len(msg),
        )
        msg = msg[:240]
    attribution_applied = bool(parsed.get("attribution_applied", False))
    return {
        "text": msg,
        "char_count": len(msg),
        "attribution_applied": attribution_applied,
        "_usage": usage,
    }


__all__ = ["compose_coordination_addressee_message"]
