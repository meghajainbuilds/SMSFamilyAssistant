"""Capability function: compose_kavi_reply — the ONE LLM compose at the end
of the intent-first dispatch (2026-06-10 rebuild). Receives the parsed
intents + executed-action results + full context and narrates what happened
(or asks the one clarifying question).

Three-axis split:
- PERSONA   -> capabilities/kavi-persona.md (injected via _build_system_prompt)
- STRUCTURE -> kavi_runtime/structural_checks.py (LENGTH_CAP_TARGET /
               LENGTH_CAP_HARD referenced in the user_msg, enforced here
               post-compose and by G-A1 downstream)
- BEHAVIOR  -> kavi-runtime/skills/kavi_reply_composer.md
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kavi_runtime.claude_client import ClaudeClient

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list, user_msg: str) -> int:
    total = sum(len(b.get("text", "")) for b in system)
    total += len(user_msg)
    return int(total / 4)


def compose_kavi_reply(
    client,
    *,
    inbound_text: str,
    sender: str,
    intents: list[dict[str, Any]],
    executed: list[dict[str, Any]],
    recent_outbound: list[dict[str, Any]] | None = None,
    recent_inbound: list[str] | None = None,
    pending_facts: list[dict[str, Any]] | None = None,
    open_coordination_sessions: list[dict[str, Any]] | None = None,
) -> str | None:
    """Compose the single user-facing reply for one inbound. Returns the
    message string or None on error/empty/over-length — the caller's cold
    fallback is ONE safe sentence, never a template that re-narrates."""
    from kavi_runtime.structural_checks import LENGTH_CAP_HARD, LENGTH_CAP_TARGET

    system = client._build_system_prompt("kavi_reply_composer")
    input_payload = {
        "inbound_text": inbound_text,
        "sender": sender,
        "intents": [
            {
                "type": i.get("type"),
                "target_text": (i.get("target_text") or "")[:200],
                "targets": i.get("targets") or [],
                "confidence": i.get("confidence"),
            }
            for i in intents
        ],
        "executed": executed,
        "recent_outbound": [
            {"kind": r.get("kind"), "text": (r.get("text") or "")[:200]}
            for r in (recent_outbound or [])[-5:]
        ],
        "recent_inbound": [str(s)[:300] for s in (recent_inbound or [])[-5:]],
        "pending_facts": [
            {"topic": f.get("topic", ""), "text": (f.get("text") or f.get("snippet") or "")[:400]}
            for f in (pending_facts or [])
        ],
    }
    # Open coordination sessions (2026-06-22): when the requester follows up
    # mid-wait ("any update?", "can you deduce from my message?"), the reply
    # must be status-only ("still waiting to hear from Max") and must NOT
    # re-narrate the addressee's pending to-dos back to the requester
    # (principle 2 — the SBP/Hollis "Max needs to contact SBP..." leak).
    if open_coordination_sessions:
        input_payload["open_coordination_sessions"] = [
            {
                "addressee": s.get("addressee") or s.get("addressee_name", ""),
                "ask": (s.get("ask") or s.get("coordination_ask") or "")[:160],
            }
            for s in open_coordination_sessions
        ]
    user_msg = (
        "Compose Kavi's ONE reply per the skill procedure. Output JSON: "
        f"{{\"message\": \"<reply, target <= {LENGTH_CAP_TARGET} chars, "
        f"hard cap {LENGTH_CAP_HARD}>\"}}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )
    model = client._model_for_call_type("compose_kavi_reply")
    started = client._log_call_start(
        "compose_kavi_reply", model, _est_input_tokens(system, user_msg),
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
        except Exception:
            usage = None
        text = resp.content[0].text if resp.content else ""
        client._log_call_done("compose_kavi_reply", model, started, usage,
                              input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        if not parsed or "message" not in parsed:
            logger.warning("compose_kavi_reply: bad output %r", text[:200])
            return None
        msg = (parsed.get("message") or "").strip()
        if not msg:
            logger.warning("compose_kavi_reply: empty message")
            return None
        if len(msg) > LENGTH_CAP_HARD:
            logger.warning("compose_kavi_reply: over-length %d chars; rejecting", len(msg))
            return None
        return msg
    except Exception as e:
        client._log_call_failed("compose_kavi_reply", started, e)
        logger.exception("compose_kavi_reply failed: %s", e)
        return None


__all__ = ["compose_kavi_reply"]
