"""coordination course-correction classifier — classify whether the
requester's mid-coordination follow-up is asking Kavi to retry the
addressee_reach.

Context: a coordination session has fired addressee_reach to (e.g.) Max
and is awaiting Max's reply. The requester (Megha) sends a follow-up
BEFORE Max replies. This is rare on the happy path, but it covers the
important failure mode behind Bug 2 (2026-05-07): Megha said "you sent
it to me, not Max - try again", and the prior code path composed an
unverified "resending now" reply via the conversational composer
without actually re-firing the send.

Returns:
  {
    "is_course_correction": bool,
    "reason": "addressee_routing | content_revision | other",
    "confidence": "high | medium | low",
    "_usage": {...},
  }

Failing-safe: API or parse error returns is_course_correction=false so
the inbound falls through to the conversational composer (the prior
behavior), avoiding a fabricated retry on a parse glitch.

Phase 4 (2026-06-02): physically moved out of
kavi_runtime/claude_client.ClaudeClient.classify_coordination_course_correction.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient, _est_input_tokens

logger = logging.getLogger(__name__)


def classify_coordination_course_correction(
    client: ClaudeClient,
    reply_text: str,
    prior_addressee_message: str,
    addressee_name: str,
    coordination_ask: str,
) -> dict[str, Any]:
    """Body of the course-correction classifier. See module docstring."""
    # Keep the prompt inline rather than spawning a new skill file: this
    # is a binary classification with one canonical few-shot, and bundling
    # it with the existing reply parser would muddy that prompt's job.
    system_text = (
        "# Skill: coordination course-correction classifier\n\n"
        "You are classifying a SECOND iMessage from the original requester "
        "in an active coordination session. The session already pinged the "
        "addressee and is awaiting their reply.\n\n"
        "Decide: is the requester telling Kavi the addressee_reach was "
        "wrong and asking him to retry (e.g., wrong addressee, wrong "
        "content)? OR is the requester just chatting / commenting / "
        "asking something unrelated?\n\n"
        "Course-correction signals:\n"
        "- 'you sent it to me, not Max'\n"
        "- 'that was meant for Max'\n"
        "- 'try again', 'resend', 'send it to <name>'\n"
        "- 'actually tell <name>'\n"
        "- 'wrong person'\n\n"
        "NOT course-correction:\n"
        "- 'thanks'\n"
        "- 'any update?' (just nudging)\n"
        "- a fresh, unrelated request\n"
        "- a question about something else\n\n"
        "When the message reads as a course-correction, set "
        "is_course_correction=true and pick a reason:\n"
        "- 'addressee_routing' - the addressee_reach went to the wrong "
        "  person / chat (Bug 1 surface).\n"
        "- 'content_revision' - the addressee got the message, but the "
        "  content was wrong and the requester wants it resent.\n"
        "- 'other' - course-correction shape but doesn't fit either bucket.\n\n"
        "Conservative bias: false-positive course-corrections re-ping the "
        "addressee unnecessarily. When in doubt, return false. Return "
        "high confidence ONLY when the signal is unambiguous.\n\n"
        "Output: ONE JSON object with fields "
        "{is_course_correction, reason, confidence}. No prose."
    )
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    input_payload = {
        "requester_followup_text": reply_text,
        "prior_addressee_message": prior_addressee_message,
        "addressee_name": addressee_name,
        "coordination_ask": coordination_ask,
    }
    user_msg = (
        "Classify the requester's follow-up per the skill above. Return "
        "ONLY one JSON object with fields {is_course_correction, reason, "
        "confidence}.\n\n"
        f"```json\n{json.dumps(input_payload, indent=2)}\n```"
    )

    fallback = {
        "is_course_correction": False,
        "reason": "other",
        "confidence": "low",
        "_usage": None,
    }
    model = client._model_for_call_type("classify_coordination_course_correction")
    started = client._log_call_start(
        "classify_coordination_course_correction", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed(
            "classify_coordination_course_correction", started, e,
        )
        logger.warning(
            "classify_coordination_course_correction API call failed: %s", e,
        )
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("classify_coordination_course_correction", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("classify_coordination_course_correction usage: %s", usage)

    parsed = client._extract_json(text)
    if parsed is None or not isinstance(parsed, dict):
        logger.warning(
            "classify_coordination_course_correction returned non-JSON: %s",
            text[:300],
        )
        return {**fallback, "_usage": usage, "_error": "parse_error"}
    is_cc = parsed.get("is_course_correction")
    if not isinstance(is_cc, bool):
        return {**fallback, "_usage": usage, "_error": "bad_is_course_correction"}
    reason = parsed.get("reason", "other")
    if reason not in {"addressee_routing", "content_revision", "other"}:
        reason = "other"
    confidence = parsed.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return {
        "is_course_correction": is_cc,
        "reason": reason,
        "confidence": confidence,
        "_usage": usage,
    }


__all__ = ["classify_coordination_course_correction"]
