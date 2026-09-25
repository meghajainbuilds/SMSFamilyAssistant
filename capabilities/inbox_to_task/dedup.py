"""inbox-to-task semantic duplicate check — Sonnet judgment LLM call.

Phase 4 (2026-06-02): physical move out of
`kavi_runtime/claude_client.ClaudeClient.check_semantic_duplicate`. The
client method becomes a thin proxy delegating to this function.

Used by the inbox-to-task webhook handler to decide whether a proposed
new task is the same as an existing recent task in MS To Do. False-
positive (skip a real new task) is treated as worse than false-negative
(create a duplicate Megha can delete), so the prompt is conservative.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list[dict[str, Any]], user_msg: str) -> int:
    try:
        system_chars = sum(len(b.get("text", "")) for b in system if isinstance(b, dict))
    except Exception:
        system_chars = 0
    return (system_chars + len(user_msg or "")) // 4


def check_semantic_duplicate(
    client: "ClaudeClient",  # type: ignore[name-defined]
    proposed_title: str,
    recent_tasks: list[dict[str, Any]],
    proposed_sender: str | None = None,
    proposed_subject: str | None = None,
) -> dict[str, Any]:
    """Check whether `proposed_title` is materially the same task as any of
    `recent_tasks` (each `{id, title}`). Sonnet judgment, conservative.

    `proposed_sender` / `proposed_subject` (optional) give the judge the
    source signal behind the proposed task: two emails from the SAME biller
    about the same recurring obligation are the same task even when one title
    says "review" and the other "pay" (the Dana-Park double-task class).

    Returns:
      {"is_duplicate": true, "matches_task_id": "<id>", "reason": "..."} OR
      {"is_duplicate": false, "reason": "..."}
    """
    if not recent_tasks:
        return {"is_duplicate": False, "reason": "no recent tasks to compare against"}

    system_text = (
        "You decide if a proposed new MS To Do task is materially the same as "
        "any existing recent task. 'Materially the same' = doing one would satisfy "
        "the other. Different ACTIONS for the same person/event/topic are NOT "
        "duplicates (pay vs. sign vs. schedule are three different tasks). "
        "BUT a recurring obligation is ONE task even if the two emails phrase the "
        "action differently: two notices from the SAME biller/sender about the SAME "
        "upcoming or outstanding payment (or the same invoice, subscription, or "
        "renewal) are the SAME task — 'review the upcoming payment from X' and "
        "'pay X' are the same obligation, not two. Use the proposed sender/subject "
        "(when given) as a strong same-source signal. Be conservative otherwise: "
        "only flag duplicate if you're confident. False-positive (skip a real new "
        "task) is worse than false-negative (create a duplicate the user can delete).\n\n"
        "Return ONLY a single JSON object, no prose:\n"
        "  {\"is_duplicate\": true, \"matches_task_id\": \"<id from recent_tasks>\", \"reason\": \"<one short sentence>\"}\n"
        "  OR\n"
        "  {\"is_duplicate\": false, \"reason\": \"<one short sentence>\"}"
    )
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload: dict[str, Any] = {
        "proposed_title": proposed_title,
        "recent_tasks": recent_tasks,
    }
    if proposed_sender:
        user_payload["proposed_sender"] = proposed_sender
    if proposed_subject:
        user_payload["proposed_subject"] = proposed_subject
    user_msg = (
        "Decide whether the proposed task is materially the same as any of the recent tasks.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    model = client._model_for_call_type("check_semantic_duplicate")
    started = client._log_call_start(
        "check_semantic_duplicate", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("check_semantic_duplicate", started, e)
        logger.warning("check_semantic_duplicate API call failed: %s", e)
        return {"is_duplicate": False, "reason": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("check_semantic_duplicate", model, started, usage,
                          input_text=user_msg, output_text=text)
    logger.info("check_semantic_duplicate usage: %s proposed=%r matched=%s",
                usage, proposed_title[:80],
                "yes" if "true" in text.lower()[:200] else "no")

    parsed = client._extract_json(text)
    if parsed is None or "is_duplicate" not in parsed:
        logger.warning("check_semantic_duplicate returned non-JSON: %s", text[:200])
        return {"is_duplicate": False, "reason": "parse_error"}
    return parsed


__all__ = ["check_semantic_duplicate"]
