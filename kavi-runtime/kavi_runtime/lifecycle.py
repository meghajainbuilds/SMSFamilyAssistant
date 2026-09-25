"""Package lifecycle state machine: title + body templates per state.

Spec source: `capabilities/inbox-to-task.md` "Package lifecycle" subsection
("Title and body templates per state" table) + Engineering checklist item 4.

Each transition function takes `(existing_task_body, email_payload, owner_abbrev)`
and returns `{"new_title": str, "new_body": str}`.

Body-update rule (per spec item 4): prepend a new dated line to an existing
audit-trail markdown sub-list within the body; preserve the rest of the body.
The audit trail is a "## Updates" markdown section. We always append at the
top of the Updates list (most-recent first) so the latest event is visible
without scrolling on Megha's phone.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from kavi_runtime.package_extractor import (
    _REJECT_SENDER_DOMAINS,
    _REJECT_SENDER_SUFFIXES,
    tracking_url as _tracking_url,
)

# Header for the audit-trail section appended to MS To Do task bodies.
_UPDATES_HEADER = "## Updates"


def _now_short() -> str:
    """Render a short timestamp (UTC) for body audit-trail lines.

    Format: `YYYY-MM-DD HH:MM UTC`. The exact wall-clock matters for OFD/
    delivered transitions, so we keep it precise rather than collapsing to
    "Wed 08:30."
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _strip_existing_state_prefix(title_body: str) -> str:
    """Remove a prior lifecycle state prefix (`Ordered: `, `Shipped: `, ...)
    from a title body so transitions don't accumulate stale prefixes.

    Operates on the post-owner-abbrev portion of the title (caller strips
    the leading `MJ ` / `MM `). Preserves the human-readable remainder.
    """
    rx = re.compile(
        r"^(?:✅\s*Delivered|❌\s*Cancelled|"
        r"Out for delivery TODAY|Ordered|Shipped|Delivered|Cancelled)\s*:\s*",
        re.IGNORECASE,
    )
    return rx.sub("", title_body, count=1)


def _prepend_audit_line(body: str, line: str) -> str:
    """Prepend `line` to the `## Updates` sub-list in `body`. If the section
    doesn't exist yet, append a fresh one at the end of body.

    Preserves the original body content above the Updates section. Each audit
    line lives as a markdown bullet (`- <ts>: <event>`).
    """
    if not body:
        body = ""
    bullet = f"- {line}"
    if _UPDATES_HEADER in body:
        # Insert directly after the header so newest is on top.
        return body.replace(_UPDATES_HEADER, f"{_UPDATES_HEADER}\n{bullet}", 1)
    sep = "\n\n" if body and not body.endswith("\n") else "\n"
    return f"{body}{sep}{_UPDATES_HEADER}\n{bullet}"


def _is_reject_domain(domain: str) -> bool:
    """True when `domain` is a freemail / forwarder / ESP sender-of-record
    domain that never names the real store (mirrors the package_extractor
    reject set). Such a domain must NOT leak into a title.
    """
    domain = (domain or "").strip().lower().strip(">").strip()
    if not domain:
        return False
    if domain in _REJECT_SENDER_DOMAINS:
        return True
    return any(
        domain == suf or domain.endswith("." + suf) for suf in _REJECT_SENDER_SUFFIXES
    )


def _merchant_label(email_payload: dict[str, Any]) -> str | None:
    """Render a human-friendly merchant label from email metadata, or None when
    no trustworthy merchant is available.

    Honors `email_payload['merchant']` only when it's a real value (the
    package_extractor returns None for reject-set domains). Then falls back to
    sender display name, then sender domain — but never a freemail / ESP domain
    ("gmail.com", "shopifyemail.com"), since those produced the "Ordered: Gmail"
    garbage titles. Returns None when nothing trustworthy is found; the caller
    builds a neutral subject-based title instead.
    """
    merchant = email_payload.get("merchant")
    if merchant:
        # Capitalize first letter so titles read "Hannaandersson" not "hannaandersson".
        return merchant[:1].upper() + merchant[1:]
    sender_name = email_payload.get("from_name") or ""
    if sender_name:
        return sender_name
    sender_addr = email_payload.get("from_address") or ""
    if "@" in sender_addr:
        domain = sender_addr.rsplit("@", 1)[1]
        if not _is_reject_domain(domain):
            return domain
    return None


def _neutral_title(owner_abbrev: str, email_payload: dict[str, Any]) -> str:
    """Build a neutral package-update title when no trustworthy merchant exists.

    Shape: "<owner> Package update — <subject>" (subject trimmed). Never renders
    "None", "Gmail", or any reject-set domain.
    """
    subject = (email_payload.get("subject") or "").strip()
    if subject:
        return f"{owner_abbrev} Package update — {subject}"
    return f"{owner_abbrev} Package update"


def _extract_title_body(existing_title: str, owner_abbrev: str) -> str:
    """Strip the owner-abbrev prefix from a stored task title and any prior
    state prefix. Returns the human-readable title body suitable for re-templating.
    """
    title = existing_title or ""
    # Drop `[?] ` low-conf marker if present (lifecycle tasks shouldn't carry it,
    # but defensive against tasks created via the LLM path).
    if title.startswith("[?] "):
        title = title[4:]
    # Drop `MJ ` / `MM ` prefix if present.
    if len(title) >= 3 and title[2] == " " and title[:2] in {"MJ", "MM"}:
        title = title[3:]
    return _strip_existing_state_prefix(title)


def _render_tracking_token(email_payload: dict[str, Any]) -> str | None:
    """Render the id portion of an audit line — either a Markdown link
    `[id](url)` when the carrier is known, or the bare id as a fallback.

    Returns None when there's no `package_id` to render at all (caller decides
    whether to emit a bare "Ordered." / "Shipped." line in that case).
    """
    package_id = email_payload.get("package_id")
    if not package_id:
        return None
    url = _tracking_url(email_payload.get("carrier"), package_id)
    if url:
        return f"[{package_id}]({url})"
    return str(package_id)


def to_ordered(
    existing_task_body: str,
    email_payload: dict[str, Any],
    owner_abbrev: str,
) -> dict[str, str]:
    """Transition into Ordered state. Used both on first-occurrence creates
    and (rarely) on a late "order placed" email arriving after a shipped one.

    When a carrier-known package_id is present, the audit line renders the id
    as a Markdown link to the carrier's tracking page (Amazon order IDs link to
    the order-details page). MS To Do renders Markdown links as tappable on
    iOS / desktop, so Megha lands on the carrier page from the task body.
    """
    merchant = _merchant_label(email_payload)
    if merchant:
        new_title = f"{owner_abbrev} Ordered: {merchant} (awaiting ship date)"
    else:
        new_title = _neutral_title(owner_abbrev, email_payload)
    token = _render_tracking_token(email_payload)
    audit = "Ordered."
    if token:
        audit = f"Ordered. Order/tracking id: {token}."
    new_body = _prepend_audit_line(existing_task_body or "", f"{_now_short()}: {audit}")
    return {"new_title": new_title, "new_body": new_body}


def to_shipped(
    existing_task_body: str,
    email_payload: dict[str, Any],
    owner_abbrev: str,
) -> dict[str, str]:
    """Transition into Shipped state. Title carries ETA when extractable from
    the email; otherwise reads "ETA TBD" rather than fabricate a date.

    When a carrier-known package_id is present, the audit line renders the id
    as a Markdown link to the carrier's tracking page (see `to_ordered` notes).
    """
    merchant = _merchant_label(email_payload)
    eta_hint = email_payload.get("eta_hint") or "TBD"
    if merchant:
        new_title = f"{owner_abbrev} Shipped: {merchant} (ETA {eta_hint})"
    else:
        new_title = f"{_neutral_title(owner_abbrev, email_payload)} (ETA {eta_hint})"
    token = _render_tracking_token(email_payload)
    audit = "Shipped."
    if token:
        audit = f"Shipped. Tracking id: {token}."
    new_body = _prepend_audit_line(existing_task_body or "", f"{_now_short()}: {audit}")
    return {"new_title": new_title, "new_body": new_body}


def to_out_for_delivery(
    existing_task_body: str,
    email_payload: dict[str, Any],
    owner_abbrev: str,
) -> dict[str, str]:
    """Transition into Out for Delivery state. This is the only transition that
    triggers an iMessage to Megha (porch-monitoring urgency). Caller is
    responsible for the iMessage; this function only renders title + body.
    """
    merchant = _merchant_label(email_payload)
    if merchant:
        new_title = f"{owner_abbrev} Out for delivery TODAY: {merchant}"
    else:
        new_title = f"{owner_abbrev} Out for delivery TODAY — {(email_payload.get('subject') or 'package').strip()}"
    new_body = _prepend_audit_line(
        existing_task_body or "",
        f"{_now_short()}: Out for delivery.",
    )
    return {"new_title": new_title, "new_body": new_body}


def to_delivered(
    existing_task_body: str,
    email_payload: dict[str, Any],
    owner_abbrev: str,
) -> dict[str, str]:
    """Transition into Delivered state. Title gets the ✅ marker but the task
    stays open in MS To Do — Megha closes manually (her 2026-05-05 decision:
    "you will not know when I actually go and pick the package").
    """
    merchant = _merchant_label(email_payload)
    if merchant:
        new_title = f"{owner_abbrev} ✅ Delivered: {merchant}"
    else:
        new_title = f"{owner_abbrev} ✅ Delivered — {(email_payload.get('subject') or 'package').strip()}"
    new_body = _prepend_audit_line(
        existing_task_body or "",
        f"{_now_short()}: Delivered.",
    )
    return {"new_title": new_title, "new_body": new_body}


def to_cancelled(
    existing_task_body: str,
    email_payload: dict[str, Any],
    owner_abbrev: str,
) -> dict[str, str]:
    """Transition into Cancelled state. Task stays open (parallels Delivered);
    Megha closes manually.
    """
    merchant = _merchant_label(email_payload)
    reason = email_payload.get("cancellation_reason") or ""
    suffix = f" — {reason}" if reason else ""
    if merchant:
        new_title = f"{owner_abbrev} ❌ Cancelled: {merchant}{suffix}"
    else:
        base = (email_payload.get("subject") or "package").strip()
        new_title = f"{owner_abbrev} ❌ Cancelled — {base}{suffix}"
    audit = "Cancelled."
    if reason:
        audit = f"Cancelled. Reason: {reason}."
    new_body = _prepend_audit_line(existing_task_body or "", f"{_now_short()}: {audit}")
    return {"new_title": new_title, "new_body": new_body}


# Dispatch table: lifecycle_state_hint string → transition function. Caller
# (handlers._apply_lifecycle_update) looks up the function by hint and applies it.
TRANSITIONS = {
    "Ordered": to_ordered,
    "Shipped": to_shipped,
    "Out for Delivery": to_out_for_delivery,
    "Delivered": to_delivered,
    "Cancelled": to_cancelled,
}


def apply_transition(
    state: str,
    existing_task_title: str,
    existing_task_body: str,
    email_payload: dict[str, Any],
    owner_abbrev: str,
) -> dict[str, str] | None:
    """Apply the transition for `state` to an existing task. Returns
    {"new_title", "new_body"} or None when `state` is not a valid lifecycle
    state. The existing title is consumed only to derive prior owner abbrev /
    body; the new title is freshly built from the template.
    """
    fn = TRANSITIONS.get(state)
    if fn is None:
        return None
    # existing_task_title is currently unused by the transition functions
    # (templates rebuild the title from scratch). Kept in the signature so
    # future templates can preserve item-level details from the prior title
    # without breaking callers.
    _ = existing_task_title
    return fn(existing_task_body, email_payload, owner_abbrev)
