"""Capability function: compose_qa_question.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.compose_qa_question(...)` keep working.

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


def compose_qa_question(
    client,
    title: str,
    source_subject: str,
    owner_abbrev: str,
) -> str | None:
    """Compose a low-confidence Q&A iMessage prompt via Sonnet using the v0.1 persona
    prompt (skill: qa_question_composer.md). Output is the natural-language question;
    Megha replies free-form (parsed by the reply intent parser). Returns None on error /
    empty / over-length so the caller can fall back to a templated message that
    still avoids the dead "1 yes / 1 no" syntax (per persona spec).
    """
    system = client._build_system_prompt("qa_question_composer")
    input_payload = {
        "title": title,
        "source_subject": source_subject,
        "owner_abbrev": owner_abbrev,
    }
    user_msg = (
        "Compose ONE low-confidence Q&A iMessage to Megha based on the input. Output JSON: "
        "{\"message\": \"<your message, <=120 chars>\"}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )
    model = client._model_for_call_type("compose_qa_question")
    started = client._log_call_start(
        "compose_qa_question", model,
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
            logger.info("compose_qa_question usage: %s", usage)
        except Exception:
            usage = None
        text = resp.content[0].text if resp.content else ""
        client._log_call_done("compose_qa_question", model, started, usage,
                             input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        if not parsed or "message" not in parsed:
            logger.warning("compose_qa_question: bad output %r", text[:200])
            return None
        msg = (parsed.get("message") or "").strip()
        if not msg:
            logger.warning("compose_qa_question: empty message")
            return None
        if len(msg) > 180:
            logger.warning("compose_qa_question: over-length %d chars; rejecting", len(msg))
            return None
        return msg
    except Exception as e:
        client._log_call_failed("compose_qa_question", started, e)
        logger.exception("compose_qa_question failed: %s", e)
        return None
