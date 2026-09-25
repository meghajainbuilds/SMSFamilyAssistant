"""Outbound content scanner + sender / recipient allowlist gates.

Implements the runtime-side security guardrails specified in
`capabilities/realtime-kavi.md` Guardrails section (the "just-shipped"
hardening rules) and inherited by Kavi coordinates per its spec:

  1. Outbound iMessage recipient allowlist — block iMessage SEND attempts to a
     handle not in `household.md`.
  2. Inbound iMessage sender allowlist — discard webhooks from handles not in
     `household.md` before any classifier or persona call.
  3. Outbound content scanner — regex-based sensitive-pattern detection on
     every outbound iMessage, MS To Do task body write, and durable-fact
     write. Runs AFTER the LLM composer and BEFORE the actual send.
  4. Durable-facts write filter — same content scanner applied to fact_text
     before `record_fact` writes.

Defense in depth: the LLM persona refusal in Kavi persona is the soft layer.
This module is the deterministic hard layer that runs independently of LLM
judgment. A persona prompt regression cannot bypass these.

All blocked outbounds append a row to `outbound_blocked.jsonl` for audit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---- household allowlist ---------------------------------------------------

# Megha's and Max's phones + email addresses, plus Kavi's own Apple ID
# (used in some chat-guid contexts). Real values live in the private
# config.yaml household block (kavi_runtime/household.py); resolved once at
# import so the gate is a plain set lookup. A config without a household
# block raises at import, so the gate can never fail open.
from kavi_runtime import household as _household

HOUSEHOLD_HANDLES: set[str] = _household.all_handles()


def _normalize_handle(handle: str | None) -> str:
    """Lower-case + strip; preserves '+' on phone numbers. None becomes ''."""
    if not handle:
        return ""
    return handle.strip().lower()


def is_household_handle(handle: str | None) -> bool:
    """Returns True when the handle (phone or email) appears in
    `household.md`'s iMessage-handles table. Case-insensitive on the email
    forms; phone numbers compare verbatim including the '+' prefix."""
    h = _normalize_handle(handle)
    if not h:
        return False
    return h in {_normalize_handle(x) for x in HOUSEHOLD_HANDLES}


# ---- sensitive-pattern regex set -------------------------------------------

# Each pattern carries a short name used in the block-reason. Adjust here
# when adding categories; tests cover the named patterns so a typo on a
# new entry breaks loudly.
_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # ---- Specific-prefix patterns first (Added 2026-05-06 evening, A6) ----
    # API-key prefixes. Most providers ship long random tails after a
    # human-readable prefix; the prefix anchor keeps false positives near
    # zero. Listed BEFORE the generic numeric patterns so a Slack token
    # like `xoxb-1234567890-...` matches `api_key_slack_bot` and not
    # `account_number` on the embedded 10-digit fragment.
    ("api_key_anthropic", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("api_key_stripe_pk", re.compile(r"\bpk_[A-Za-z0-9_]{20,}")),
    ("api_key_aws_access", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("api_key_github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("api_key_slack_bot", re.compile(r"\bxoxb-[A-Za-z0-9_\-]{40,}")),
    # 2FA-code shape: 6-8 consecutive digits in a "code is X" /
    # "verification code: X" / "your X code" context. The window scopes
    # to a small set of leading phrases so context-free numerics (zip
    # codes, order ids) don't match. Case-insensitive.
    (
        "two_factor_code",
        re.compile(
            r"(?i)\b(?:code\s+is|verification\s+code|auth\s+code|"
            r"security\s+code|your\s+\w{1,16}\s+code|one[- ]?time\s+code)"
            r"[\s:]{1,4}\d{6,8}\b",
        ),
    ),
    # Bearer token in body context: "Bearer <token>" with at least 20 chars
    # of base64-ish payload. Catches OAuth bearers leaking into LLM-rendered
    # text. Listed before the generic numeric patterns.
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b"),
    ),
    # ---- Generic numeric patterns ----
    # Credit card: 13-19 digits with optional spaces or dashes between groups.
    # Liberal on grouping to catch both "4111-1111-1111-1111" and unspaced
    # "4111111111111111" forms. The 13-19 length covers Amex (15), Visa /
    # MC / Discover (16), and the rare 13/19-digit variants.
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    # SSN: 9 digits as 3-2-4 with dashes.
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # US bank routing number: 9 digits standalone (not embedded in a longer
    # number). Word-boundary anchored so a phone number doesn't false-match.
    ("routing_number", re.compile(r"\b\d{9}\b")),
    # Account number: 10-12 digits standalone. Conservative — short numbers
    # (zip codes, prices) are excluded by the lower bound.
    ("account_number", re.compile(r"\b\d{10,12}\b")),
]


def scan_for_sensitive(text: str) -> tuple[bool, str | None]:
    """Run every sensitive-pattern regex on `text`. Returns (is_blocked,
    pattern_name). On a hit, pattern_name is one of the names in
    `_SENSITIVE_PATTERNS`. On no hit, returns (False, None).

    Order matters: credit-card runs first because its broad digit-grouping
    pattern would otherwise match a routing or account number embedded in a
    longer card-shaped string. The first hit wins.
    """
    if not text:
        return (False, None)
    for name, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return (True, name)
    return (False, None)


# ---- outbound_blocked.jsonl audit ------------------------------------------


def _blocked_path(config: dict | None) -> Path:
    """Resolve the outbound-blocked audit JSONL path. Defaults to a sibling
    of the eval surface so daily review can include blocks without a separate
    fetch. Falls back to `/Users/kavi/HomeOS/outbound_blocked.jsonl` when no
    config or no key present."""
    if config:
        paths = config.get("paths") or {}
        raw = paths.get("outbound_blocked_jsonl")
        if raw:
            return Path(raw)
    return Path("/Users/kavi/HomeOS/outbound_blocked.jsonl")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_fingerprint(text: str | None, reason: str) -> dict[str, Any]:
    """Render an audit-safe fingerprint of the blocked content. Returns
    fields suitable for direct merge into the audit row. The reason string
    is parsed for the sensitive-pattern tail (`sensitive_pattern_<name>` →
    `<name>`); when present we re-run that pattern against the text so the
    audit row carries the match count without persisting the raw matches.

    Fields:
      content_sha256 — hex digest, full content. Stable across runs so a
        repeat-attempt can be deduped post-hoc.
      preview_first_80_chars — leading chars only, NOT a 500-char body. The
        80-char window is enough to identify the shape (e.g., "Pay invoice
        4111 1111 1111 1111 today") in the rare case the leading chars
        themselves contain the pattern; the dedicated content_sha256 +
        match_count fields are the audit-trail truth.
      matched_pattern — the named pattern from `_SENSITIVE_PATTERNS` that
        tripped the gate (e.g., "credit_card"); None when reason is not a
        sensitive-pattern reason (e.g., unknown_recipient).
      match_count — how many times the named pattern occurs in the full
        text. Useful for distinguishing a one-off leak from a repeated
        leak, without storing the matches.
      content_length — full character length of the raw content.
    """
    raw = text or ""
    sha = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest() if raw else ""

    # Extract the named pattern from `sensitive_pattern_<name>` reasons.
    # Any suffix added by the caller (e.g., `_email_context`) is stripped
    # by re-matching the pattern name list.
    matched_pattern: str | None = None
    match_count = 0
    if reason and reason.startswith("sensitive_pattern_"):
        tail = reason[len("sensitive_pattern_"):]
        for name, pattern in _SENSITIVE_PATTERNS:
            if tail == name or tail.startswith(name + "_"):
                matched_pattern = name
                if raw:
                    match_count = len(pattern.findall(raw))
                break

    return {
        "content_sha256": sha,
        "preview_first_80_chars": raw[:80],
        "matched_pattern": matched_pattern,
        "match_count": match_count,
        "content_length": len(raw),
    }


def log_blocked(
    *,
    config: dict | None,
    surface: str,
    reason: str,
    text: str | None = None,
    recipient: str | None = None,
    sender: str | None = None,
    extras: dict[str, Any] | None = None,
) -> None:
    """Append one row to `outbound_blocked.jsonl`. Failure-safe: any IO error
    is logged and swallowed so a blocked outbound never crashes the caller.

    `surface` is one of {"imessage", "todo_body", "durable_fact",
    "inbound_imessage", "outbound_email"}. `reason` is one of
    {"unknown_recipient", "unknown_sender", "sensitive_pattern_<name>"}.

    Audit-row content shape (changed 2026-05-06 evening, A3 of audit
    follow-up): instead of a 500-char `text_preview` of the very content
    the scanner kept out, the row stores `content_sha256` + an 80-char
    leading preview + `matched_pattern` + `match_count` + `content_length`.
    The audit log keeps enough signal to triage a block without persisting
    the leaked payload itself. Existing 500-char rows from prior runs stay
    readable — the schema change is additive at read time (consumers must
    tolerate either shape).
    """
    try:
        path = _blocked_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "surface": surface,
            "reason": reason,
            "recipient": recipient,
            "sender": sender,
            "extras": extras or {},
        }
        record.update(_content_fingerprint(text, reason))
        with path.open("a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning("outbound_scanner.log_blocked failed (non-fatal): %s", e)


# ---- public gates ----------------------------------------------------------


def gate_outbound_recipient(
    *,
    config: dict | None,
    recipient: str,
    text: str | None = None,
) -> tuple[bool, str | None]:
    """Outbound iMessage recipient allowlist gate. Returns (allowed,
    block_reason). When allowed=False, the caller must NOT send and should
    log via `log_blocked` (this function does that automatically before
    returning)."""
    if is_household_handle(recipient):
        return (True, None)
    log_blocked(
        config=config,
        surface="imessage",
        reason="unknown_recipient",
        text=text,
        recipient=recipient,
    )
    logger.warning(
        "outbound_scanner: blocked iMessage send to non-household recipient=%s",
        recipient,
    )
    return (False, "unknown_recipient")


def gate_inbound_sender(
    *,
    config: dict | None,
    sender: str | None,
    text: str | None = None,
) -> tuple[bool, str | None]:
    """Inbound iMessage sender allowlist gate. Returns (allowed, reason).
    When allowed=False the caller MUST discard the payload before any
    classifier / persona / coordination call."""
    if is_household_handle(sender):
        return (True, None)
    log_blocked(
        config=config,
        surface="inbound_imessage",
        reason="unknown_sender",
        text=text,
        sender=sender,
    )
    logger.warning(
        "outbound_scanner: discarded inbound iMessage from non-household sender=%s",
        sender,
    )
    return (False, "unknown_sender")


def gate_outbound_content(
    *,
    config: dict | None,
    text: str,
    surface: str,
    recipient: str | None = None,
) -> tuple[bool, str | None]:
    """Outbound content scanner. Runs the sensitive-pattern regex set on
    `text`. Returns (allowed, block_reason). When allowed=False, the caller
    MUST NOT send / write."""
    blocked, pattern_name = scan_for_sensitive(text)
    if not blocked:
        return (True, None)
    reason = f"sensitive_pattern_{pattern_name}"
    log_blocked(
        config=config,
        surface=surface,
        reason=reason,
        text=text,
        recipient=recipient,
    )
    logger.warning(
        "outbound_scanner: blocked %s outbound on pattern=%s recipient=%s",
        surface, pattern_name, recipient,
    )
    return (False, reason)


def gate_durable_fact_write(
    *,
    config: dict | None,
    fact_text: str,
) -> tuple[bool, str | None]:
    """Durable-facts write filter. Same regex set as content scanner; reasons
    appear under surface=`durable_fact` in the audit log. Returns (allowed,
    block_reason)."""
    blocked, pattern_name = scan_for_sensitive(fact_text)
    if not blocked:
        return (True, None)
    reason = f"sensitive_pattern_{pattern_name}"
    log_blocked(
        config=config,
        surface="durable_fact",
        reason=reason,
        text=fact_text,
    )
    logger.warning(
        "outbound_scanner: blocked durable-fact write on pattern=%s",
        pattern_name,
    )
    return (False, reason)
