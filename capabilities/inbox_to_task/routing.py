"""Which model judges an email (2026-09-24).

Most email goes to the cheap model. Emails from senders that matter (school,
childcare, people writing directly) go straight to the strong model. When
the cheap model skips an email whose subject says it usually needs action
(a LinkedIn message, a delivery, a bill), the strong model takes a second
look before anything is skipped.

The actual sender lists and subject patterns are household-specific, so they
live in the private config (`inbox_to_task.judge_routing`), never in code.
No config block = every email goes to the strong model (pre-2026-09-24
behavior).

Pure functions, no IO.
"""

from __future__ import annotations

import re
from typing import Any

STRONG = "strong"
CHEAP = "cheap"

_REPLY_OR_FORWARD = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)


def _sender(email: dict[str, Any]) -> str:
    return (email.get("from_address") or "").strip().lower()


def _matches_sender(addr: str, entries: list[str]) -> bool:
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    for e in entries or []:
        e = e.lower()
        if addr == e or domain == e or domain.endswith("." + e) or ("@" not in e and e in addr):
            return True
    return False


def first_route(email: dict[str, Any], routing: dict[str, Any] | None,
                keep: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return (route, why). `keep` is the newsletter keep list: anything it
    protects also goes to the strong model."""
    if not routing:
        return STRONG, "no_routing_config"
    if routing.get("strong_on_reply_or_forward", True) and \
            _REPLY_OR_FORWARD.match(email.get("subject") or ""):
        return STRONG, "reply_or_forward"
    if _matches_sender(_sender(email), routing.get("strong_senders") or []):
        return STRONG, "strong_sender"
    if keep is not None:
        from kavi_runtime.inbox_pre_filter import keep_rule
        rule = keep_rule(email, keep)
        if rule:
            return STRONG, rule
    return CHEAP, "default"


def needs_second_look(email: dict[str, Any], decision: dict[str, Any],
                      routing: dict[str, Any] | None) -> bool:
    """True when the cheap model skipped an email whose subject usually
    means action. The strong model then decides."""
    if not routing or decision.get("status") != "skipped":
        return False
    subject = email.get("subject") or ""
    return any(re.search(p, subject, re.IGNORECASE)
               for p in routing.get("second_look_subjects") or [])
