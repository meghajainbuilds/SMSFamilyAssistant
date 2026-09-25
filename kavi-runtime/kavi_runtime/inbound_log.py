"""Inbound message logger — eval surface for what Kavi receives.

Every inbound iMessage from Megha gets one append-only row in
eval-persona-inbound.jsonl. Pairs with eval-persona-outbound-judgments.jsonl:
each outbound row records `triggered_by: <inbound_id>` so the weekly HTML-viewer
trace can render the inbound text alongside Kavi's reply (the chat-based daily
slash command surface retired 2026-05-27).

Cron-driven outbound (periodic_summary, weekly_self_check, alert) has no
triggering inbound — its triggered_by is null.

The current_inbound_id contextvar is set by handlers.imessage_received at the
top of the function and read by outbound_log.log_outbound when it runs. The
contextvar propagates across asyncio.to_thread (used by server.py to dispatch
imessage_received off the event loop), so threading happens automatically.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.state import append_jsonl

logger = logging.getLogger(__name__)


current_inbound_id: ContextVar[str | None] = ContextVar("current_inbound_id", default=None)


def _inbound_path(config: dict) -> Path:
    p = Path(config["paths"]["eval_persona_inbound_jsonl"])
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _inbound_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"i_{ts}_{uuid.uuid4().hex[:8]}"


def log_inbound(
    config: dict,
    *,
    text: str,
    sender: str | None = None,
    source: str = "imessage",
    context: dict[str, Any] | None = None,
) -> str:
    """Append one inbound row, set current_inbound_id contextvar, return the id.

    Failure-safe: any exception is logged and swallowed. We still set and return
    an id even on write failure so triggered_by linkage stays consistent across
    the request even if the inbound JSONL is unwritable for a moment.
    """
    iid = _inbound_id()
    try:
        record = {
            "inbound_id": iid,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "capability": "kavi-persona",
            "source": source,
            "sender": sender,
            "text": text,
            "char_count": len(text),
            "context": context or {},
        }
        append_jsonl(_inbound_path(config), record)
    except Exception as e:
        logger.exception("inbound_log: failed to log err=%s", e)
    current_inbound_id.set(iid)
    return iid
