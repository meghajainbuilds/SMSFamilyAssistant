"""Package-id and lifecycle-state extractor.

Pure function (no IO, no SDK, no Graph). Given an email's `subject`, `body`, and
`sender`, returns:
  {
    "package_id": str | None,
    "carrier": "ups" | "usps" | "fedex" | "amazon" | "shopify" | None,
    "merchant": str | None,
    "lifecycle_state_hint": "Ordered" | "Shipped" | "Out for Delivery"
                          | "Delivered" | "Cancelled" | None,
  }

`carrier` is inferred from which id-format regex matched the package_id. It
drives the carrier-specific tracking URL rendered in lifecycle audit lines
(see `tracking_url`). When carrier is None or the id shape is unknown, the
audit line falls back to plain-text id rendering.

Spec source: `capabilities/inbox-to-task.md` "Engineering checklist" item 1
(2026-05-05 package-lifecycle build). Patterns are stable; reuse directly when
extending tier-2 heuristics.
"""

from __future__ import annotations

import re
from typing import Any

# Order of these patterns matters: first match wins. Amazon (and Whole Foods on
# Amazon's order infrastructure) shares a 3-7-7 dash pattern that is highly
# distinctive and we prefer it over generic carrier patterns when both could
# match the same blob of text.
_AMAZON_ORDER_RE = re.compile(r"\b1\d{2}-\d{7}-\d{7}\b")
_UPS_RE = re.compile(r"\b1Z[A-Z0-9]{16}\b")
# USPS first because FedEx's 12/15-digit pattern would otherwise greedily steal
# the 12-digit shape from a 20+ digit USPS string.
_USPS_RE = re.compile(r"\b\d{20,22}\b")
_FEDEX_RE = re.compile(r"\b\d{12}(?:\d{3})?\b")
# Shopify / generic store order numbers (e.g. "#2121341", "order number 1955038").
# SCOPED to an explicit "order #" / "order number" cue so we never match a bare
# 6-8 digit run elsewhere in marketing prose. Captures the digits in group 1.
_SHOPIFY_ORDER_RE = re.compile(
    r"order\s*(?:#|no\.?|number|num)?\s*#?\s*(\d{6,8})\b",
    re.IGNORECASE,
)

# Sender domains that NEVER name the real merchant. Two families:
#   - freemail / personal forwarders: a forwarded order confirmation arrives from
#     a person's own mailbox, so the domain is "gmail" not the store.
#   - email-service-provider sender-of-record domains: Shopify et al. send store
#     mail from their own infra (store+...@t.shopifyemail.com), so the domain is
#     the ESP ("shopifyemail") not the store.
# When the sender domain matches, _merchant_from_sender returns None and the
# caller/title falls back to from_name / subject brand.
_REJECT_SENDER_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "icloud.com",
    "me.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "shopifyemail.com",
    "t.shopifyemail.com",
}
# Any subdomain of these is also rejected (e.g. "em.shopifyemail.com").
_REJECT_SENDER_SUFFIXES = (
    "shopifyemail.com",
)

# Lifecycle-state keyword cues, ordered most-specific → least. Cancelled is
# checked first so a "your shipped order has been cancelled" phrasing routes
# correctly. OFD is checked before Shipped because "out for delivery" contains
# no "shipped" substring but its phrasing can co-occur in marketing prose.
_STATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcancel(?:l?ed|lation)\b", re.IGNORECASE), "Cancelled"),
    (re.compile(r"\b(?:has been )?delivered\b", re.IGNORECASE), "Delivered"),
    (re.compile(r"\bout for delivery\b", re.IGNORECASE), "Out for Delivery"),
    (re.compile(r"\b(?:has )?shipped\b|on (?:its|the) way", re.IGNORECASE), "Shipped"),
    (re.compile(
        r"\border (?:placed|received|confirm(?:ed|ation))|"
        r"thanks? (?:you )?for your order|"
        r"your .*? order has been received",
        re.IGNORECASE,
    ), "Ordered"),
]


def _merchant_from_sender(sender: str) -> str | None:
    """Pull a normalized merchant token from the sender domain.

    Strategy: take the email domain, drop common subdomain prefixes
    ("mail", "e", "info", "order-update", "ship-confirm", "auto-confirm",
    "noreply", "no-reply"), and drop the TLD. Returns the registrable label
    when one is identifiable, else None.

    Whole Foods (which uses amazon order infrastructure) carries a sender-side
    hint via subject keywords; for email-source merchant matching we use the
    domain. The order_id pattern handles cross-merchant identification under
    Amazon's umbrella anyway.
    """
    if not sender:
        return None
    sender = sender.strip().lower()
    if "@" in sender:
        domain = sender.rsplit("@", 1)[1]
    else:
        domain = sender
    domain = domain.strip().strip(">").strip()
    # Reject freemail / forwarder / ESP sender-of-record domains: the domain on
    # these never names the store, so returning None lets the title fall back to
    # from_name / subject brand instead of emitting "Ordered: Gmail/Shopifyemail".
    if domain in _REJECT_SENDER_DOMAINS:
        return None
    if any(domain == suf or domain.endswith("." + suf) for suf in _REJECT_SENDER_SUFFIXES):
        return None
    parts = [p for p in domain.split(".") if p]
    if not parts:
        return None
    # Drop 2-letter TLDs and common 3-letter TLDs from the right.
    while len(parts) > 1 and parts[-1] in {
        "com", "net", "org", "co", "io", "us", "biz", "info", "shop",
    }:
        parts.pop()
    # Drop common subdomain prefixes from the left until we hit something
    # that looks like a real merchant token.
    while len(parts) > 1 and parts[0] in {
        "mail", "e", "email", "m", "info", "order", "orders", "order-update",
        "ship", "ship-confirm", "shipment", "auto-confirm", "noreply",
        "no-reply", "do-not-reply", "donotreply", "auto", "store", "www",
    }:
        parts.pop(0)
    if not parts:
        return None
    # Take the last (rightmost) remaining label; that's typically the brand.
    return parts[-1]


def extract(*, subject: str, body: str, sender: str) -> dict[str, Any]:
    """Extract package_id, merchant, lifecycle_state_hint from an email.

    Inputs are strings (subject + body + sender email address). Output keys are
    always present; values are None when no signal is detected. Pure function:
    safe to call inside `email_arrived` before any IO.
    """
    subject = subject or ""
    body = body or ""
    sender = sender or ""
    text = f"{subject}\n{body}"

    package_id: str | None = None
    carrier: str | None = None
    # The carrier label aligns with which id-shape regex matched, since each
    # carrier-specific URL template (`tracking_url`) only knows how to render
    # ids of its own family. Amazon order IDs are tracked at amazon.com/order-details,
    # which is an order-status URL not a carrier-tracking URL, but Megha treats
    # both the same way ("tap the id, see status") so we surface them under the
    # same carrier slot.
    for rx, label in (
        (_AMAZON_ORDER_RE, "amazon"),
        (_UPS_RE, "ups"),
        (_USPS_RE, "usps"),
        (_FEDEX_RE, "fedex"),
    ):
        m = rx.search(text)
        if m:
            package_id = m.group(0)
            carrier = label
            break

    # Fallback: Shopify / generic store order numbers. Only fires when no
    # carrier id matched, and only when an explicit "order #" cue precedes the
    # 6-8 digit run (scoped to avoid matching bare digit runs in prose). This
    # gives tier-1 exact-id matching something to key on for Shopify merchants
    # whose order numbers (#2121341) match none of the carrier shapes.
    if package_id is None:
        m = _SHOPIFY_ORDER_RE.search(text)
        if m:
            package_id = m.group(1)
            carrier = "shopify"

    merchant = _merchant_from_sender(sender)

    lifecycle_state_hint: str | None = None
    # Match against subject first (highest signal), then body.
    for src in (subject, body):
        if not src:
            continue
        for rx, state in _STATE_PATTERNS:
            if rx.search(src):
                lifecycle_state_hint = state
                break
        if lifecycle_state_hint:
            break

    return {
        "package_id": package_id,
        "carrier": carrier,
        "merchant": merchant,
        "lifecycle_state_hint": lifecycle_state_hint,
    }


# Carrier tracking URL templates. Keys match the `carrier` values returned by
# `extract`. Values are format strings with a single `{id}` placeholder.
_TRACKING_URL_TEMPLATES: dict[str, str] = {
    "ups": "https://www.ups.com/track?tracknum={id}",
    "usps": "https://tools.usps.com/go/TrackConfirmAction?tLabels={id}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={id}",
    # Amazon order IDs only — not carrier-from-Amazon tracking IDs (those don't
    # have a stable public lookup; Megha lands on the order-details page instead).
    "amazon": "https://www.amazon.com/gp/your-account/order-details?orderID={id}",
}


def tracking_url(carrier: str | None, package_id: str | None) -> str | None:
    """Return the carrier-specific tracking URL for a package id, or None when
    the carrier is unknown or the id is missing.

    The id-shape was already validated by `extract` (each carrier label is set
    only after its regex matched), so we don't re-validate format here. Callers
    that synthesize (carrier, id) pairs outside of `extract` are responsible
    for their own validation.
    """
    if not carrier or not package_id:
        return None
    template = _TRACKING_URL_TEMPLATES.get(carrier.lower())
    if not template:
        return None
    return template.format(id=package_id)
