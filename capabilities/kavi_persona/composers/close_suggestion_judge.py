"""close-suggestion judge — LLM judgment entry point (added 2026-06-10).

Decides whether one of the recipient's own SENT email replies shows that
the underlying action behind an open task was already taken — i.e., the
task is probably done and the evening rollup may SUGGEST closing it.

Called from the evening close-suggestion selector
(`capabilities/kavi_persona/close_suggestions.py`) for each
(open task, newest sent reply) pair that has not been judged before.
The selector owns all cost caps (max judgments per run, judged-pair
cache); this module owns only the single LLM call.

Returns:
  {
    "suggest_close": bool,
    "reason": str | None,       # one line, grounded in the reply text
    "confidence": "high" | "medium" | "low",
    "_usage": {...},
  }

Failing-safe AND conservative by design: API error, parse error, or any
malformed field returns suggest_close=false. A missed suggestion costs
nothing (Megha closes the task herself, as she does today); a wrong
suggestion teaches her to ignore the feature. The selector additionally
requires confidence == "high" before surfacing — "when unsure, do NOT
suggest" is enforced in code, not just in the skill prompt.

Pattern-matched on capabilities/coordination/composers/intent_classifier.py
(same structure, logging, and fallback shape).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient, _est_input_tokens

logger = logging.getLogger(__name__)

# Defensive cap on the reply text shipped to the judge. Graph's sentitems
# bodyPreview is ~255 chars; this only matters if a future caller passes a
# full body.
_REPLY_PREVIEW_CAP = 500


def judge_close_suggestion(
    client: ClaudeClient,
    task_title: str,
    task_created_at: str,
    reply_preview: str,
    reply_sent_at: str,
    reply_direction: str = "sent",
) -> dict[str, Any]:
    """Body of the close-suggestion judge. See module docstring.

    WORST-CASE NIGHTLY TOKEN COST (computed 2026-06-10): the selector caps
    judgments at CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN = 8 per evening run.
    Per judgment: system = skill text (~700 tokens incl. wrapper) + user
    payload (title ≤120 chars + reply preview ≤500 chars + two timestamps
    ≈ 200 tokens) ≈ 900 input tokens; output ≤ 150 tokens (max_tokens=300
    bound). 8 judgments × (900 in + 150 out) = 7,200 input + 1,200 output
    per night worst case. At Sonnet pricing ($3/M in, $15/M out) that is
    ~$0.022 + ~$0.018 ≈ $0.04/night ≈ $1.20/month worst case — and the
    skill block is prompt-cached after the first call, so the realistic
    nightly figure is lower.
    """
    skill = client._skill("close_suggestion_judge")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload = {
        "task_title": (task_title or "")[:120],
        "task_created_at": task_created_at,
        "reply_preview": (reply_preview or "")[:_REPLY_PREVIEW_CAP],
        "reply_sent_at": reply_sent_at,
        # "sent" = the recipient's own reply handling the task; "inbound" =
        # the other party's reply in the thread (e.g. a vendor confirming).
        # An inbound confirmation is a strong done-signal but be conservative:
        # "we received your payment" => done; "did you pay?" => NOT done.
        "reply_direction": reply_direction if reply_direction in ("sent", "inbound") else "sent",
    }
    user_msg = (
        "Judge this (open task, sent reply) pair per the skill procedure. "
        "Return ONLY a single JSON object with fields "
        "{suggest_close, reason, confidence}. No prose around it.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    fallback = {
        "suggest_close": False,
        "reason": None,
        "confidence": "low",
        "_usage": None,
    }

    model = client._model_for_call_type("judge_close_suggestion")
    started = client._log_call_start("judge_close_suggestion", model,
                                     _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("judge_close_suggestion", started, e)
        logger.warning("judge_close_suggestion API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("judge_close_suggestion", model, started, usage,
                          input_text=user_msg, output_text=text)
    logger.info("judge_close_suggestion usage: %s", usage)

    parsed = client._extract_json(text)
    if parsed is None or not isinstance(parsed, dict):
        logger.warning("judge_close_suggestion returned non-JSON: %s", text[:300])
        return {**fallback, "_usage": usage, "_error": "parse_error"}

    suggest = parsed.get("suggest_close")
    if not isinstance(suggest, bool):
        return {**fallback, "_usage": usage, "_error": "bad_suggest_close"}
    confidence = parsed.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    reason = parsed.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = None

    return {
        "suggest_close": suggest,
        "reason": reason,
        "confidence": confidence,
        "_usage": usage,
    }


__all__ = ["judge_close_suggestion"]
