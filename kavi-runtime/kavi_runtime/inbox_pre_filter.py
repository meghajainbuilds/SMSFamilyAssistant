"""Inbox pre-filter (REC-1, audits/token_optimization_2026-05-06.md).

A deterministic short-circuit that runs BEFORE `claude.run_email_to_tasks`.
When an email matches the marketing/newsletter signature (sender domain in
the seeded denylist, `List-Unsubscribe` header present, or
`Auto-Submitted: auto-generated` header present) the pre-filter would skip
the LLM call entirely.

SHADOW MODE (default, 2026-05-06):
  Today the pre-filter does NOT actually skip. It logs every match to a
  shadow audit JSONL alongside the LLM's actual decision so Megha can
  compare for 7 days. After Megha reviews and confirms zero false-skip
  candidates (real action items the denylist would have eaten), she replies
  "promote pre-filter to live" and a future agent flips
  `inbox_pre_filter.shadow_mode` to False in `config.yaml`. With
  `shadow_mode: false`, `email_arrived` will short-circuit on a True return
  and write a synthetic `skipped` row to `eval-inbox-judgments.jsonl` —
  same audit shape as a hard-rule skip — before returning.

The seeded denylist comes from the audit's top-15 marketing senders that
the LLM already unanimously skipped. Header-based detection adds a second
layer that catches any sender that respects RFC 2369 / RFC 3834 even if
they're not on the denylist.

Public API:
  would_pre_filter_skip(email_payload) -> tuple[bool, str | None]
  write_shadow_log(metrics_dir, row) -> None

Both functions are pure / IO-isolated so they're trivially testable.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Seeded denylist from audits/token_optimization_2026-05-06.md REC-1.
# Top-15 senders by 7-day call volume; the LLM unanimously skipped every
# email from these domains. Match is suffix-based on the from_address (so
# `news@parentmap.com` and `noreply@parentmap.com` both hit `parentmap.com`).
# Owned in code (not config) for v0 — once the list grows past ~50 we'll
# move it to a YAML alongside `priority_senders` so Megha can edit without
# a runtime restart.
SENDER_DOMAIN_DENYLIST: frozenset[str] = frozenset({
    # Top 7 from the audit (REC-1 seed list)
    "e.m.hannaandersson.com",
    "mail.vogue.com",
    "eml.nordstrom.com",
    "linkedin.com",  # jobalerts-noreply@linkedin.com — full LinkedIn job alerts
    "parentmap.com",
    "morning7.theskimm.com",
    "emails.farfetch.com",
    # Audit reference data: the additional marketing senders in the 7-day
    # corpus that round out the top-15 most common skip senders. Each is
    # a domain the LLM has skipped 5+ times in 7 days with reason="marketing"
    # or reason="newsletter".
    "news.athleta.com",
    "email.gap.com",
    "email.oldnavy.com",
    "email.bananarepublic.com",
    "e.bananarepublic.com",
    "email.target.com",
    "email.amazon.com",  # amazon marketing domain (transactional uses ses-handler)
    "alerts.indeed.com",
    "info.craigslist.org",
})

# Sender-domain denylist explanation for each entry's match reason. Kept
# outside the frozenset so denylist iteration order doesn't matter.
SHADOW_REASON_DENYLIST = "denylist_sender_domain"
SHADOW_REASON_LIST_UNSUBSCRIBE = "header_list_unsubscribe"
SHADOW_REASON_AUTO_SUBMITTED = "header_auto_submitted_auto_generated"


def _from_address_lower(email_payload: dict[str, Any]) -> str:
    """Defensive: payload may be empty or missing the field."""
    if not isinstance(email_payload, dict):
        return ""
    return (email_payload.get("from_address") or "").strip().lower()


def _domain_of(addr: str) -> str:
    """Return the full domain of an email address (`x@y.z` -> `y.z`)."""
    if not addr or "@" not in addr:
        return ""
    return addr.split("@", 1)[1].strip().lower()


def _domain_matches_denylist(domain: str) -> bool:
    """Return True if `domain` exactly equals or is a subdomain of any
    denylist entry. Subdomain match catches `bounces.email.amazon.com` when
    `email.amazon.com` is on the list."""
    if not domain:
        return False
    if domain in SENDER_DOMAIN_DENYLIST:
        return True
    for entry in SENDER_DOMAIN_DENYLIST:
        if domain.endswith("." + entry):
            return True
    return False


def _internet_headers(email_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of internetMessageHeaders if present (Graph schema)
    or an empty list. Defensive against missing or non-list values."""
    if not isinstance(email_payload, dict):
        return []
    headers = email_payload.get("internetMessageHeaders") or []
    if not isinstance(headers, list):
        return []
    return headers


def _has_header(email_payload: dict[str, Any], name: str,
                 value_substring: str | None = None) -> bool:
    """Case-insensitive check for `name` in internetMessageHeaders. When
    `value_substring` is provided, also requires that substring to appear
    (case-insensitive) in the header value. Microsoft Graph exposes
    `internetMessageHeaders` as a list of `{name, value}` dicts."""
    name_l = (name or "").strip().lower()
    sub_l = (value_substring or "").strip().lower() if value_substring else None
    for h in _internet_headers(email_payload):
        if not isinstance(h, dict):
            continue
        h_name = (h.get("name") or "").strip().lower()
        if h_name != name_l:
            continue
        if sub_l is None:
            return True
        h_val = (h.get("value") or "").strip().lower()
        if sub_l in h_val:
            return True
    return False


def would_pre_filter_skip(
    email_payload: dict[str, Any],
) -> tuple[bool, str | None]:
    """Returns (True, reason) if the email matches a pre-filter rule;
    otherwise (False, None).

    Matched rules (in order):
      1. Sender domain in `SENDER_DOMAIN_DENYLIST` (exact or subdomain).
      2. `List-Unsubscribe` header present.
      3. `Auto-Submitted: auto-generated` header present.

    The function is pure: same input always returns the same output; no
    IO; no logging. Caller decides whether to write a shadow audit row
    or skip the LLM (depending on `shadow_mode`).

    Defensive: an empty / None / non-dict `email_payload` returns
    (False, None) so a malformed webhook never short-circuits a real
    email through this gate.
    """
    if not isinstance(email_payload, dict) or not email_payload:
        return (False, None)

    addr = _from_address_lower(email_payload)
    domain = _domain_of(addr)
    if _domain_matches_denylist(domain):
        return (True, SHADOW_REASON_DENYLIST)

    if _has_header(email_payload, "List-Unsubscribe"):
        return (True, SHADOW_REASON_LIST_UNSUBSCRIBE)

    if _has_header(email_payload, "Auto-Submitted", value_substring="auto-generated"):
        return (True, SHADOW_REASON_AUTO_SUBMITTED)

    return (False, None)


def keep_rule(email_payload: dict[str, Any], keep: dict[str, Any] | None) -> str | None:
    """Return the name of the keep rule that protects this email, or None.

    The keep list (config `inbox_pre_filter.keep`, approved by Megha
    2026-09-24) overrides every filter rule: a kept email always reaches
    the LLM judge. It exists because shadow data showed the filter would
    have silently dropped school, childcare, insurance, LinkedIn
    person-to-person and order-status emails the judge turned into tasks.
    """
    if not keep or not isinstance(email_payload, dict):
        return None
    addr = _from_address_lower(email_payload)
    domain = _domain_of(addr)
    subject = email_payload.get("subject") or ""

    if addr in {a.lower() for a in keep.get("sender_addresses") or []}:
        return "keep_sender_address"
    for entry in keep.get("sender_domains") or []:
        entry = entry.lower()
        if domain == entry or domain.endswith("." + entry):
            return "keep_sender_domain"
    if any(s.lower() in addr for s in keep.get("sender_substrings") or []):
        return "keep_sender_substring"
    if domain in {d.lower() for d in keep.get("personal_domains") or []}:
        return "keep_personal_sender"
    for pattern in keep.get("subject_patterns") or []:
        if re.search(pattern, subject, re.IGNORECASE):
            return "keep_subject"
    return None


def shadow_log_path(metrics_dir: Path) -> Path:
    """Resolve the shadow audit JSONL path under the configured metrics dir."""
    return Path(metrics_dir) / "inbox_pre_filter_shadow.jsonl"


def write_shadow_log(
    metrics_dir: Path,
    *,
    email_id: str,
    sender: str,
    subject: str,
    reason: str,
    llm_decision: str | None = None,
    llm_reason: str | None = None,
    kept_by: str | None = None,
) -> None:
    """Append one JSON row per shadow_skip to
    `<metrics_dir>/inbox_pre_filter_shadow.jsonl`.

    Schema:
        {ts, email_id, sender, subject, reason, llm_decision, llm_reason}

    `llm_decision` / `llm_reason` are filled in once the (still-running)
    LLM call returns, so the shadow row records "what would have been
    skipped" alongside "what the LLM actually decided" — the comparison
    Megha will make to gate promote-to-live after 7 days.

    Never raises — a shadow log write failure must not crash the runtime.
    """
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "email_id": email_id,
        "sender": sender,
        "subject": subject,
        "reason": reason,
        "llm_decision": llm_decision,
        "llm_reason": llm_reason,
        # Non-null = a keep rule would have let this email through to the
        # LLM once the filter is live. Promotion math counts only null rows.
        "kept_by": kept_by,
    }
    try:
        path = shadow_log_path(metrics_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        logger.warning("inbox_pre_filter shadow write failed (continuing): %s", e)
