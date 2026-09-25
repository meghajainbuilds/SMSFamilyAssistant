"""Capability function: compose_conversational_reply.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.compose_conversational_reply(...)` keep working.

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


def compose_conversational_reply(
    client,
    inbound_text: str,
    recent_outbound: list[dict[str, Any]] | None = None,
    pending_facts: list[dict[str, Any]] | None = None,
) -> str | None:
    """G-C1 + G-C2: respond to a conversational inbound from Megha (one that
    wasn't a Q&A reply, client-check answer, or correction). Composes a natural
    reply via persona prompt (skill: kavi_conversation.md). Caller passes the
    last 3-5 outbound rows from eval-persona-outbound-judgments.jsonl as thread
    context (G-C2 simple version). Returns the message string (≤180 char hard
    cap) or None on error/empty/over-length so the caller can use a minimal
    cold fallback rather than send nothing.

    `pending_facts` (added 2026-06-10): `{topic, snippet}` rows from the
    selection feed (`capabilities/kavi_persona/selection.py:
    _read_pending_facts_for_summary`) so the composer can answer "what is
    the task?" follow-ups about facts the digest offered. Before this, the
    composer had empty fact context and Megha's "Yea" to a digest offer
    dead-ended. Behavior rules for when/how to use this context live in
    kavi-runtime/skills/kavi_conversation.md (three-axis split).
    """
    system = client._build_system_prompt("kavi_conversation")
    recent = []
    for r in (recent_outbound or [])[-5:]:
        recent.append({
            "ts": r.get("ts"),
            "kind": r.get("kind"),
            "text": (r.get("text") or "")[:200],
        })
    input_payload = {
        "recent_outbound": recent,
        "inbound_text": inbound_text,
        # Full fact text — selection owns pre-filtering; no re-capping here
        # (same rule as the periodic_summary composer, 2026-06-10).
        "pending_facts": [
            {
                "topic": (pf.get("topic") or ""),
                "snippet": (pf.get("snippet") or ""),
            }
            for pf in (pending_facts or [])
        ],
    }
    user_msg = (
        "Compose ONE conversational reply to Megha based on the input. Output JSON: "
        "{\"message\": \"<your reply, <=120 chars>\"}. Nothing else.\n\n"
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>"
    )
    model = client._model_for_call_type("compose_conversational")
    started = client._log_call_start(
        "compose_conversational", model,
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
            logger.info("compose_conversational_reply usage: %s", usage)
        except Exception:
            usage = None
        text = resp.content[0].text if resp.content else ""
        client._log_call_done("compose_conversational", model, started, usage,
                             input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        if not parsed or "message" not in parsed:
            logger.warning("compose_conversational_reply: bad output %r", text[:200])
            return None
        msg = (parsed.get("message") or "").strip()
        if not msg:
            logger.warning("compose_conversational_reply: empty message")
            return None
        if len(msg) > 180:
            logger.warning("compose_conversational_reply: over-length %d chars; rejecting", len(msg))
            return None
        return msg
    except Exception as e:
        client._log_call_failed("compose_conversational", started, e)
        logger.exception("compose_conversational_reply failed: %s", e)
        return None

# ---- Kavi coordinates capability methods (added 2026-05-05) ----
#
# Phase 4 (2026-06-02) physical move: each method body lives in
# `capabilities/coordination/composers/<name>.py`. These methods are
# thin proxies kept on ClaudeClient for backwards compatibility with
# existing callers (`claude.classify_coordination_intent(...)` etc.).
# The proxies do a lazy import to avoid the circular
# capabilities.coordination -> kavi_runtime.claude_client -> capabilities
# cycle at module-load time.
