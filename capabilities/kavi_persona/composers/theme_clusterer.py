"""morning theme clusterer — LLM judgment entry point (added 2026-06-10).

Looks at the recipient's open task titles (plus creation dates and today's
date for seasonality) and names AT MOST ONE top-of-mind theme — a cluster
of at least THEME_MIN_CLUSTER open tasks that belong to the same effort
(e.g., many camp-related tasks as summer approaches). Zero themes is a
valid and common output; the skill instructs the model that a forced weak
theme is worse than none.

Called once per morning per recipient by the theme selector
(`capabilities/kavi_persona/selection.py:_select_morning_theme`), which
owns the cost caps (input capped at THEME_MAX_INPUT_TITLES titles, each
truncated to THEME_TITLE_TRUNCATE_CHARS chars) and the per-day cache.

Returns:
  {
    "theme": {"label": str, "supporting_task_titles": [str, ...]} | None,
    "_usage": {...},
  }

Failing-safe: API or parse error returns theme=None with an `_error` key
so the selector can distinguish "the model judged no theme" (cacheable
for the day) from "the call failed" (not cached; tomorrow retries).

Pattern-matched on capabilities/coordination/composers/intent_classifier.py
(same structure, logging, and fallback shape).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient, _est_input_tokens

logger = logging.getLogger(__name__)


def cluster_morning_theme(
    client: ClaudeClient,
    task_titles: list[str],
    today_date: str,
) -> dict[str, Any]:
    """Body of the morning theme clusterer. See module docstring.

    WORST-CASE MORNING TOKEN COST (computed 2026-06-10): the selector caps
    input at THEME_MAX_INPUT_TITLES = 40 titles × THEME_TITLE_TRUNCATE_CHARS
    = 80 chars = 3,200 chars ≈ 800 tokens, plus skill text (~600 tokens
    incl. wrapper) and scaffolding ≈ 1,500 input tokens; output ≤ 200
    tokens (max_tokens=400 bound). The per-day cache means at most ONE call
    per recipient per morning → 2 calls worst case (Megha + Max):
    3,000 input + 400 output per morning. At Sonnet pricing ($3/M in,
    $15/M out) ≈ $0.009 + $0.006 ≈ $0.015/morning ≈ $0.45/month worst
    case; prompt caching of the skill block lowers the realistic figure.
    """
    skill = client._skill("morning_theme_clusterer")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload = {
        "today_date": today_date,
        "open_task_titles": list(task_titles or []),
    }
    user_msg = (
        "Cluster these open task titles per the skill procedure. Return "
        "ONLY a single JSON object with field {theme} where theme is "
        "either null or {label, supporting_task_titles}. No prose around "
        "it.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    fallback: dict[str, Any] = {"theme": None, "_usage": None}

    model = client._model_for_call_type("cluster_morning_theme")
    started = client._log_call_start("cluster_morning_theme", model,
                                     _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("cluster_morning_theme", started, e)
        logger.warning("cluster_morning_theme API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("cluster_morning_theme", model, started, usage,
                          input_text=user_msg, output_text=text)
    logger.info("cluster_morning_theme usage: %s", usage)

    parsed = client._extract_json(text)
    if parsed is None or not isinstance(parsed, dict) or "theme" not in parsed:
        logger.warning("cluster_morning_theme returned non-JSON: %s", text[:300])
        return {**fallback, "_usage": usage, "_error": "parse_error"}

    theme = parsed.get("theme")
    if theme is None:
        return {"theme": None, "_usage": usage}
    if not isinstance(theme, dict):
        return {**fallback, "_usage": usage, "_error": "bad_theme_shape"}

    label = theme.get("label")
    supporting = theme.get("supporting_task_titles")
    if not isinstance(label, str) or not label.strip():
        return {**fallback, "_usage": usage, "_error": "bad_theme_label"}
    if not isinstance(supporting, list) or not all(isinstance(t, str) for t in supporting):
        return {**fallback, "_usage": usage, "_error": "bad_supporting_titles"}

    return {
        "theme": {
            "label": label.strip(),
            "supporting_task_titles": supporting,
        },
        "_usage": usage,
    }


__all__ = ["cluster_morning_theme"]
