"""Capability function: compose_weekly_self_check, classify_self_check_reply.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.compose_weekly_self_check(...)` keep working.

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


def compose_weekly_self_check(client) -> str | None:
    """Friday client-check (Stage 4). Compose a cognitive-load question via Sonnet
    using the v0.1 persona prompt (skill: weekly_self_check_composer.md). Returns
    the composed question (≤180 char hard cap), or None on error / over-length /
    empty output. Caller (weekly_self_check.send_weekly_self_check) does NOT fall
    back to a template on None — better to miss a week than send a survey-style
    question. Persona spec rule: default-to-LLM where Kavi talks to a person.
    """
    system = client._build_system_prompt("weekly_self_check_composer")
    user_msg = (
        "Compose ONE weekly client-check question for iMessage to Megha. Output JSON: "
        "{\"message\": \"<your message, <=120 chars>\"}. Nothing else."
    )
    model = client._model_for_call_type("compose_self_check_question")
    started = client._log_call_start(
        "compose_self_check_question", model,
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
            logger.info("compose_weekly_self_check usage: %s", usage)
        except Exception:
            usage = None
        text = resp.content[0].text if resp.content else ""
        client._log_call_done("compose_self_check_question", model, started, usage,
                             input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        if not parsed or "message" not in parsed:
            logger.warning("compose_weekly_self_check: bad output %r", text[:200])
            return None
        msg = (parsed.get("message") or "").strip()
        if not msg:
            logger.warning("compose_weekly_self_check: empty message")
            return None
        if len(msg) > 180:
            logger.warning("compose_weekly_self_check: over-length %d chars; rejecting", len(msg))
            return None
        return msg
    except Exception as e:
        client._log_call_failed("compose_self_check_question", started, e)
        logger.exception("compose_weekly_self_check failed: %s", e)
        return None


def classify_self_check_reply(client, reply_text: str, question: str) -> dict[str, Any]:
    """Classify Megha's free-form reply to the Friday client-check question.

    Returns:
      {"is_self_check_reply": bool,
       "rating": "saved" | "added" | "neutral" | "unclear" | None,
       "rationale": str,
       "_usage": {...}}

    On parse error or bad rating: returns is_self_check_reply=false so the caller
    falls through to existing routing (correction classifier). Failing-safe:
    we never claim a client-check rating we couldn't justify.
    """
    skill = client._skill("weekly_self_check_classifier")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload = {
        "pending_question": question,
        "megha_reply": reply_text,
    }
    user_msg = (
        "Classify this reply per the skill procedure. Return ONLY a single JSON object "
        "with fields {is_self_check_reply, rating, rationale}. No prose around it.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    model = client._model_for_call_type("classify_self_check_reply")
    started = client._log_call_start("classify_self_check_reply", model,
                                    _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("classify_self_check_reply", started, e)
        logger.warning("classify_self_check_reply API call failed: %s", e)
        return {"is_self_check_reply": False, "rating": None, "rationale": f"api_error: {e}", "_usage": None}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("classify_self_check_reply", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("classify_self_check_reply usage: %s", usage)

    parsed = client._extract_json(text)
    if parsed is None or "is_self_check_reply" not in parsed:
        logger.warning("classify_self_check_reply returned non-JSON: %s", text[:200])
        return {"is_self_check_reply": False, "rating": None, "rationale": "parse_error", "_usage": usage}
    if not isinstance(parsed.get("is_self_check_reply"), bool):
        logger.warning("classify_self_check_reply bad bool: %r", parsed.get("is_self_check_reply"))
        return {"is_self_check_reply": False, "rating": None, "rationale": "bad_bool", "_usage": usage}
    rating = parsed.get("rating")
    if parsed["is_self_check_reply"] and rating not in {"saved", "added", "neutral", "unclear"}:
        logger.warning("classify_self_check_reply bad rating: %r", rating)
        parsed["rating"] = "unclear"
    if not parsed["is_self_check_reply"]:
        parsed["rating"] = None
    parsed["_usage"] = usage
    return parsed
