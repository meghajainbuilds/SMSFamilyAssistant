"""coordination ack composer — Kavi's immediate ack to the requester.

Compose Kavi's ack to the requester within seconds of an inbound
coordination request landing. Per acceptance criterion #1 of the
capability spec: ack within 10 seconds. Brief: "Got it, checking with
Max now. I'll let you know what he says."

Re-uses the persona system prompt (kavi_conversation skill) for voice
consistency with the rest of Kavi's outbound surface. Returns
{"text": str | None, "char_count": int, "_usage": {...}}.

Phase 4 (2026-06-02): physically moved out of
kavi_runtime/claude_client.ClaudeClient.compose_coordination_ack.
"""

from __future__ import annotations

import json
from typing import Any

from kavi_runtime.claude_client import ClaudeClient


def compose_coordination_ack(
    client: ClaudeClient,
    inbound_text: str,
    addressee_name: str,
) -> dict[str, Any]:
    """Body of the coordination ack composer. See module docstring."""
    system = client._build_system_prompt("kavi_conversation")
    input_payload = {
        "inbound_text": inbound_text,
        "addressee_name": addressee_name,
        "ack_purpose": "coordination_ack",
    }
    user_msg = (
        "Compose ONE quick ack to the requester acknowledging that you're starting "
        f"the coordination check with {addressee_name}. <=120 chars. Output JSON: "
        "{\"message\": \"<your ack>\"}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )
    return client._voice_capped_call(
        user_msg, system, "compose_coordination_ack",
        max_tokens=200, char_cap=180, call_type="compose_coordination_ack",
    )


__all__ = ["compose_coordination_ack"]
