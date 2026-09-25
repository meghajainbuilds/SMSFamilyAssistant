"""coordination outcome composer — Kavi's outcome message back to the requester.

Per principle 2: outcome-only, no internal-mechanic narration.

Returns {"text": str | None, "char_count": int, "_usage": {...}}.
Free-form judgment over the branch — the composer is told to NOT narrate
task creation, scheduler ops, or durable-facts writes.

F3 (2026-05-07): branch `4b_will_grab_task_create_failed` is the narrow
exception to principle 2. When the addressee committed but the
task-tracking POST raised, the composer MUST tell the requester because
silently shipping "Max said he'll grab it" drops a task Megha believes is
in the system. `task_create_failure_reason` carries the short error
string for the composer to summarize.

Phase 4 (2026-06-02): physically moved out of
kavi_runtime/claude_client.ClaudeClient.compose_coordination_outcome.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient, _est_input_tokens

logger = logging.getLogger(__name__)


def compose_coordination_outcome(
    client: ClaudeClient,
    branch: str,
    commitment_text: str | None,
    task_id_if_created: str | None,
    task_title_if_created: str | None,
    requester_name: str,
    addressee_name: str,
    task_create_failure_reason: str | None = None,
) -> dict[str, Any]:
    """Body of the coordination outcome composer. See module docstring."""
    skill = client._skill("coordination_outcome_composer")
    # Persona-voiced outbound (relay back to requester). Inherit the
    # refusal layer (2026-05-05) so a coordination outcome that quotes
    # untrusted addressee text still respects the categorical never-do
    # list and the social-engineering refusal language.
    system_text = f"# Skill\n\n{skill}"
    system = client._persona_system_block(system_text)

    input_payload = {
        "branch": branch,
        "commitment_text": commitment_text,
        "task_id_if_created": task_id_if_created,
        "task_title_if_created": task_title_if_created,
        "requester_name": requester_name,
        "addressee_name": addressee_name,
        "task_create_failure_reason": task_create_failure_reason,
    }
    user_msg = (
        "Compose ONE outcome iMessage to the requester per the skill procedure. "
        "Output JSON: {\"message\": \"<your outcome, <=120 chars>\"}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )

    fallback = {"text": None, "char_count": 0, "_usage": None}
    model = client._model_for_call_type("compose_coordination_outcome")
    started = client._log_call_start(
        "compose_coordination_outcome", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("compose_coordination_outcome", started, e)
        logger.exception("compose_coordination_outcome API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("compose_coordination_outcome", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("compose_coordination_outcome usage: %s", usage)

    parsed = client._extract_json(text)
    if not parsed or "message" not in parsed:
        logger.warning("compose_coordination_outcome: bad output %r", text[:200])
        return {**fallback, "_usage": usage, "_error": "parse_error"}
    msg = (parsed.get("message") or "").strip()
    if msg == "REFUSED_PERSONA_CATEGORICAL":
        return {**fallback, "_usage": usage, "_error": "refused_persona"}
    if not msg:
        return {**fallback, "_usage": usage, "_error": "empty_message"}
    if len(msg) > 180:
        logger.warning("compose_coordination_outcome: over-length %d chars; truncating", len(msg))
        msg = msg[:180]
    return {"text": msg, "char_count": len(msg), "_usage": usage}


__all__ = ["compose_coordination_outcome"]
