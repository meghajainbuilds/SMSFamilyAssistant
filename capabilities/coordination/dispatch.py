"""Coordination intent dispatch — entry point called from imessage_received.

When Megha sends a free-text iMessage like "ask Max about the card", this
dispatcher (1) runs the coordination-intent classifier to confirm the
inbound is a coordination request with high confidence, (2) defends against
non-household addressees and self-loops, then (3) hands off to
`coordination_handler.start_coordination` which acks the requester, messages
the addressee, and waits for the addressee's reply (which routes back
through imessage_received's coordination-reply branch).

Returns a status dict to short-circuit the conversational composer; or None
when the inbound is NOT a coordination request (caller falls through to
compose_conversational_reply).

Lives in `capabilities/coordination/` because the routing decision IS the
coordination capability's entry point.
"""

from __future__ import annotations

import logging
from typing import Any

from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.graph_client import GraphClient
from kavi_runtime.runtime.clients import _get_clients

logger = logging.getLogger(__name__)


def _try_handle_coordination_intent(
    *,
    free_text: str,
    sender_handle: str | None,
    claude: ClaudeClient,
    graph: GraphClient,
    config: dict,
) -> dict[str, Any] | None:
    """Kavi coordinates routing (added 2026-05-05). Runs the coordination-
    intent classifier on `free_text`. When it returns is_coordination=true
    with confidence=high, starts a coordination session via the coordination
    handler — that path acks the requester, messages the addressee, and
    waits for the addressee's reply (which routes back through
    imessage_received's coordination-reply branch).

    Returns a status dict to short-circuit the conversational composer; or
    None when the inbound is NOT a coordination request (caller falls
    through to compose_conversational_reply).
    """
    from capabilities.coordination import handler as coordination_handler

    requester_handle = sender_handle or ""
    members = coordination_handler._household_members_for_classifier(config)
    intent = claude.classify_coordination_intent(
        free_text=free_text,
        requester_handle=requester_handle,
        household_members=members,
    )
    is_coord = intent.get("is_coordination", False)
    confidence = intent.get("confidence", "low")
    if not is_coord or confidence != "high":
        # Not a coordination request, OR confidence too low to act on. Fall
        # through to the conversational composer (per the conservative bias
        # documented in the classifier skill: false-positive coordinations
        # are worse than false-negatives).
        return None

    addressee_name = intent.get("addressee_name") or ""
    addressee_handle = intent.get("addressee_handle") or ""
    coordination_ask = intent.get("coordination_ask") or free_text

    # Defensive: if the addressee handle isn't a household handle, refuse to
    # engage. The classifier shouldn't return a non-household handle, but
    # the deterministic gate is the second line of defense.
    from kavi_runtime.runtime import outbound_scanner
    if not outbound_scanner.is_household_handle(addressee_handle):
        logger.warning(
            "_try_handle_coordination_intent: classifier returned non-household addressee=%s; falling through",
            addressee_handle,
        )
        return None

    # Don't coordinate with self.
    if addressee_handle.strip().lower() == (requester_handle or "").strip().lower():
        return None

    _, _, bb = _get_clients(config)
    result = coordination_handler.start_coordination(
        inbound_text=free_text,
        requester_handle=requester_handle,
        addressee_handle=addressee_handle,
        addressee_name=addressee_name,
        coordination_ask=coordination_ask,
        config=config,
        claude=claude,
        graph=graph,
        bb=bb,
    )
    return result


__all__ = ["_try_handle_coordination_intent"]
