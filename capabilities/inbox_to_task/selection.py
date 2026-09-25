"""inbox-to-task selection layer — which emails reach the LLM classifier.

Phase 4 (2026-06-02): physical move. The function bodies for
`_normalize_email`, `_build_thread_state`, `_inbox_owner_abbrev`,
`_account_to_owner_name` live here, not behind a re-export of
`kavi_runtime/handlers.py`. Pre-filter (`kavi_runtime/inbox_pre_filter.py`)
stays where it is — it's the deterministic gate that runs before the
LLM classifier.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# Re-export the deterministic pre-filter for one-cd Investigator access.
from kavi_runtime.inbox_pre_filter import *  # noqa: F401,F403
from kavi_runtime import inbox_pre_filter as _inbox_pre_filter

_PT = ZoneInfo("America/Los_Angeles")


def _received_pt_anchor(received_iso: str) -> str | None:
    """Deterministic Pacific calendar anchor for relative-date resolution.

    Parses the email's received timestamp (ISO 8601, MS Graph emits a `Z`
    suffix) and renders it in America/Los_Angeles as
    "YYYY-MM-DD (Weekday) PT". The downstream compose prompt anchors
    weekday->date math ("by Monday", "due Friday") on this actual Pacific
    calendar day instead of resolving relative dates unanchored.

    Returns None if the timestamp is missing or unparseable — never guesses.
    """
    if not received_iso:
        return None
    raw = received_iso.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    local = dt.astimezone(_PT)
    return f"{local:%Y-%m-%d} ({local:%A}) PT"


def _normalize_email(msg: dict[str, Any]) -> dict[str, Any]:
    """Convert MS Graph message to email_to_tasks input shape."""
    body = msg.get("body", {}).get("content") or msg.get("bodyPreview", "")
    if len(body) > 4000:
        body = body[:4000] + "\n[truncated]"
    sender = msg.get("from", {}).get("emailAddress", {})
    # internetMessageHeaders pass-through (added 2026-05-06): used by the
    # inbox pre-filter (REC-1 SHADOW mode) to detect List-Unsubscribe +
    # Auto-Submitted before the LLM call.
    headers = msg.get("internetMessageHeaders") or []
    received = msg.get("receivedDateTime", "")
    return {
        "id": msg["id"],
        "subject": msg.get("subject", ""),
        "from_name": sender.get("name", ""),
        "from_address": sender.get("address", ""),
        "to": [r["emailAddress"]["address"] for r in msg.get("toRecipients", [])],
        "received": received,
        # Deterministic Pacific calendar anchor (2026-06-22): lets the
        # compose prompt resolve relative dates ("by Monday") against the
        # email's actual PT day instead of unanchored weekday math.
        "received_pt": _received_pt_anchor(received),
        "body_text": body,
        "internetMessageHeaders": headers,
    }


def _build_thread_state(
    thread_inbound: list[dict[str, Any]],
    thread_sent: list[dict[str, Any]],
    inbound_received_dt: str,
) -> dict[str, Any]:
    user_replies = [
        {
            "sent_at": m.get("sentDateTime", ""),
            "body_preview": (m.get("bodyPreview") or "")[:300],
            "to": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
        }
        for m in thread_sent
        if m.get("sentDateTime", "") > inbound_received_dt
    ]
    return {
        "user_replies": user_replies,
        "thread_message_count": len(thread_inbound) + len(thread_sent),
    }


def _inbox_owner_abbrev(config: dict, source_account: str | None = None) -> str:
    """Inbox-based owner for package lifecycle tasks. Resolves the
    Microsoft account the email arrived on to the task-prefix abbreviation
    written into MS To Do task titles.
    """
    if not source_account:
        return "MJ"
    from kavi_runtime import household
    a = source_account.strip().lower()
    if a in {e.lower() for e in household.member_emails("megha")}:
        return "MJ"
    if a in {e.lower() for e in household.member_emails("max")}:
        return "MM"
    return "MJ"


def _account_to_owner_name(source_account: str | None) -> str:
    """Plain-language owner name for the account that received an email."""
    if not source_account:
        return "megha"
    from kavi_runtime import household
    a = source_account.strip().lower()
    if a in {e.lower() for e in household.member_emails("max")}:
        return "max"
    return "megha"


__all__ = list(getattr(_inbox_pre_filter, "__all__", [])) + [
    "_received_pt_anchor",
    "_normalize_email",
    "_build_thread_state",
    "_inbox_owner_abbrev",
    "_account_to_owner_name",
]
