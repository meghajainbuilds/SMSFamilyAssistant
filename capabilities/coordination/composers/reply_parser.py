"""coordination reply parser — parses the addressee's reply into a branch
decision per the canonical Rosa cash few-shot.

Returns the structured branch + commitment + task title + deadline +
confidence + reasoning.

Failing-safe: API or parse error returns branch=4c_ambiguous so the runtime
asks the addressee to clarify rather than acting on a fabricated parse.

Phase 4 (2026-06-02): physically moved out of
kavi_runtime/claude_client.ClaudeClient.parse_coordination_reply.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient, _est_input_tokens

logger = logging.getLogger(__name__)


def parse_coordination_reply(
    client: ClaudeClient,
    reply_text: str,
    prior_addressee_message: str,
    addressee_name: str,
    coordination_ask: str,
) -> dict[str, Any]:
    """Body of the coordination reply parser. See module docstring."""
    skill = client._skill("coordination_reply_parser")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    input_payload = {
        "prior_addressee_message": prior_addressee_message,
        "reply_text": reply_text,
        "addressee_name": addressee_name,
        "coordination_ask": coordination_ask,
    }
    user_msg = (
        "Parse the addressee's reply per the skill procedure. Return ONLY a single JSON "
        "object with fields {branch, commitment_text, task_title_proposal, deadline, "
        "confidence, reasoning}. No prose around it.\n\n"
        f"```json\n{json.dumps(input_payload, indent=2)}\n```"
    )

    fallback = {
        "branch": "4c_ambiguous",
        "commitment_text": "unclear",
        "task_title_proposal": None,
        "deadline": None,
        "confidence": "low",
        "reasoning": "fallback (api or parse error)",
        "_usage": None,
    }
    model = client._model_for_call_type("parse_coordination_reply")
    started = client._log_call_start("parse_coordination_reply", model,
                                    _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("parse_coordination_reply", started, e)
        logger.exception("parse_coordination_reply API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("parse_coordination_reply", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("parse_coordination_reply usage: %s", usage)

    parsed = client._extract_json(text)
    if parsed is None or not isinstance(parsed, dict):
        logger.warning("parse_coordination_reply returned non-JSON: %s", text[:300])
        return {**fallback, "_usage": usage, "_error": "parse_error"}

    branch = parsed.get("branch")
    if branch not in {"4a_yes_have_it", "4b_will_grab", "4c_ambiguous"}:
        logger.warning("parse_coordination_reply bad branch=%r; defaulting to 4c", branch)
        branch = "4c_ambiguous"
    confidence = parsed.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    commitment_text = parsed.get("commitment_text")
    task_title_proposal = parsed.get("task_title_proposal")
    deadline = parsed.get("deadline")
    reasoning = parsed.get("reasoning", "") or ""
    if not isinstance(reasoning, str):
        reasoning = ""

    return {
        "branch": branch,
        "commitment_text": commitment_text if isinstance(commitment_text, str) else None,
        "task_title_proposal": task_title_proposal if isinstance(task_title_proposal, str) else None,
        "deadline": deadline if isinstance(deadline, str) else None,
        "confidence": confidence,
        "reasoning": reasoning[:400],
        "_usage": usage,
    }


__all__ = ["parse_coordination_reply"]
