"""Capability function: classify_correction.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.classify_correction(...)` keep working.

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


def classify_correction(client, free_text: str, recent_kavi_messages: list[dict[str, Any]] | None = None,
                        recent_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Classify a free-text iMessage as a correction or off-topic noise.

    Returns one of two shapes per skill spec:
      {"status": "correction", "correction": {type, target, target_pattern, reason, new_value?}}
      {"status": "not_correction", "reason": "..."}

    On parse error, returns a not_correction so the caller skips action and logs.
    """
    # Classifier-only call: opt out of the persona refusal layer. The
    # output is a structured JSON decision, not a Kavi-voiced message.
    system = client._build_system_prompt(
        "correction_classifier", with_refusal_layer=False, with_capability_doc=True,
    )
    user_payload = {
        "free_text": free_text,
        "recent_kavi_messages": recent_kavi_messages or [],
        "recent_runs": recent_runs or [],
    }
    user_msg = (
        "Classify this iMessage per the skill procedure. Return ONLY a single JSON object "
        "matching one of the two output shapes. No prose around it.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    model = client._model_for_call_type("classify_correction")
    started = client._log_call_start("classify_correction", model,
                                    _est_input_tokens(system, user_msg))
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=client._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("classify_correction", started, e)
        raise

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("classify_correction", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("classify_correction usage: %s", usage)

    parsed = client._extract_json(text)
    if parsed is None:
        logger.warning("classify_correction returned non-JSON: %s", text[:300])
        return {"status": "not_correction", "reason": "parse_error", "_usage": usage}
    parsed["_usage"] = usage
    return parsed
