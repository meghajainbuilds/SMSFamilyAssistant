"""Financial-domain sender patterns for inbox-to-task body redaction.

Spec source: `capabilities/realtime-kavi.md` Guardrails row 4 (Task body
redaction). When an email arrives from a financial-domain sender (banks,
brokerages, insurers, IRS, schools collecting payment), the resulting MS
To Do task body is paraphrased only — sender + subject summary + a
"balance available, see source email" pointer. Verbatim numerics, account
identifiers, and dollar amounts in the body are stripped out before the
task is written. The TASK TITLE keeps the human-readable description so
Megha can recognize the task at a glance, but dollar amounts in the title
are also stripped through the same scanner so a number that survives
into the title doesn't leak through the title surface.

Patterns are conservative and additive — easier to extend a list than to
debug a regression where a benign sender was flagged. When a new financial
domain surfaces in production, append a row here and add a row to the
test suite.

The detector returns True on any match; ordering doesn't matter for
correctness, only for short-circuit cost.
"""

from __future__ import annotations

import re

# ---- regex patterns --------------------------------------------------------

# Each regex matches the FROM-address (lowercased) of an inbound email.
# Patterns intentionally cover both the sender domain (`example.com`) and
# common subdomain shapes (`mail.example.com`, `notifications.example.com`).
_FINANCIAL_DOMAIN_PATTERNS: list[re.Pattern[str]] = [
    # Banks (national + regional).
    re.compile(r"@(?:[a-z0-9-]+\.)*chase(?:bank)?\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*bankofamerica\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*wellsfargo\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*usbank\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*citibank\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*citi\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*pnc\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*tdbank\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*ally\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*capitalone\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*amex\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*americanexpress\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*discover\.com$", re.I),
    # Brokerages + retirement.
    re.compile(r"@(?:[a-z0-9-]+\.)*vanguard\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*fidelity\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*schwab\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*etrade\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*robinhood\.com$", re.I),
    # Generic bank / brokerage patterns (catch-all for domains we miss).
    re.compile(r"@(?:[a-z0-9-]+\.)*\.bank$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*brokerage(?:\.com)?$", re.I),
    # Insurers.
    re.compile(r"@(?:[a-z0-9-]+\.)*statefarm\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*allstate\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*geico\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*progressive\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*libertymutual\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*kaiserpermanente\.org$", re.I),
    # Government tax / treasury surfaces.
    re.compile(r"@(?:[a-z0-9-]+\.)*irs\.gov$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*treasury\.gov$", re.I),
    # Health systems billing — common in Megha's inbox (UW Medicine surfaced
    # the rule in the original incident).
    re.compile(r"@(?:[a-z0-9-]+\.)*uwmedicine\.org$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*uw\.edu$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*kaiser(?:permanente)?\.org$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*kp\.org$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*swedish\.org$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*virginiamason\.org$", re.I),
    # School payment portals (Boonli school lunch is the recurring case).
    # Conservative scope: only the billing surfaces, not general school mail
    # (school newsletters carry no financial detail).
    re.compile(r"@(?:[a-z0-9-]+\.)*boonli\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*tads\.com$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*facts(?:mgt)?\.com$", re.I),
    # Generic billing / payment patterns.
    re.compile(r"@(?:[a-z0-9-]+\.)*billing\.[a-z0-9-]+\.[a-z]{2,}$", re.I),
    re.compile(r"@(?:[a-z0-9-]+\.)*payments?\.[a-z0-9-]+\.[a-z]{2,}$", re.I),
]


# Dollar-amount regex used to strip dollar amounts from titles and bodies on
# financial-domain matches. Matches `$<digits>[.<digits>]` and `$<digits>,<digits>`
# forms with optional decimal places. Word-boundary anchored so an unrelated
# integer in a sentence isn't pulled in.
_DOLLAR_AMOUNT_RE = re.compile(r"\$\s?\d{1,3}(?:[,\d]{3,})*(?:\.\d{2})?\b")


def is_financial_domain_sender(from_address: str | None) -> bool:
    """Returns True if `from_address` matches a known financial-domain
    pattern. Case-insensitive; tolerant of None / empty.
    """
    if not from_address:
        return False
    addr = from_address.strip().lower()
    if "@" not in addr:
        return False
    for pattern in _FINANCIAL_DOMAIN_PATTERNS:
        if pattern.search(addr):
            return True
    return False


def strip_dollar_amounts(text: str) -> str:
    """Replace every dollar amount in `text` with the literal string
    `[redacted: $$$]` so the title/body still reads grammatically while
    making the redaction visible to Megha. Returns the modified string.

    Conservative: only strips the dollar-amount itself, not the surrounding
    sentence. Other context (vendor name, subject) survives so the title
    still tells Megha what the task is about.
    """
    if not text:
        return text
    return _DOLLAR_AMOUNT_RE.sub("[redacted: $$$]", text)


# ---- spoofed-sender v0 detection ------------------------------------------
#
# Spec source: 2026-05-06 audit, Fix 2. A phishing-shaped email arrives
# with a brand/authority token in the display name ("IRS Treasury",
# "Chase Bank", "Apple Support") but the from-address domain is NOT
# legitimate for that brand. The runtime stamps spoof_suspected=True so
# the resulting task carries `[VERIFY SENDER]` in the title and the
# audit-log row carries `spoof_suspected: <brand>`.
#
# v0 simplification:
# - Tokens are matched against the lowercased display name as substrings
#   with word boundaries. "IRS" matches "IRS Treasury" but not "IRSL"
#   (a brand fragment in a longer word would false-positive otherwise).
# - The legitimate-domain map is small and conservative (~12 entries).
#   When in doubt, we under-detect rather than spam Megha with verify
#   prefixes on benign mail. New brands are added on-demand by appending
#   to BRAND_LEGIT_DOMAINS.
# - Domain comparison is suffix-based ("subdomain.irs.gov" matches
#   "irs.gov") so legitimate notification subdomains pass.

# Ordered for readability; tokens are lowercased on match.
BRAND_LEGIT_DOMAINS: dict[str, list[str]] = {
    "irs": ["irs.gov"],
    "treasury": ["treasury.gov", "irs.gov"],
    "bank": [],  # generic; only flagged when paired with a known bank brand
    "paypal": ["paypal.com"],
    "amazon": ["amazon.com", "amazon.co.uk"],
    "apple": ["apple.com", "icloud.com", "me.com"],
    "microsoft": ["microsoft.com", "outlook.com", "live.com", "hotmail.com"],
    "google": ["google.com", "gmail.com", "googlemail.com"],
    "chase": ["chase.com", "chasebank.com", "jpmorgan.com"],
    "wells fargo": ["wellsfargo.com"],
    "bofa": ["bankofamerica.com", "bofa.com"],
    "bank of america": ["bankofamerica.com", "bofa.com"],
}


def _domain_of(address: str | None) -> str:
    if not address or "@" not in address:
        return ""
    return address.strip().lower().rsplit("@", 1)[1]


def _matches_legit_domain(addr_domain: str, legit_domains: list[str]) -> bool:
    """Suffix-match the from-address domain against any of the brand's
    legitimate domains. `accounts.irs.gov` matches `irs.gov`; `irs-gov.com`
    does not (the suffix check requires `.<legit>` or full equality)."""
    if not addr_domain or not legit_domains:
        return False
    for legit in legit_domains:
        legit_norm = legit.strip().lower()
        if not legit_norm:
            continue
        if addr_domain == legit_norm or addr_domain.endswith("." + legit_norm):
            return True
    return False


def detect_spoof(from_name: str | None, from_address: str | None) -> tuple[bool, str | None]:
    """Return (spoof_suspected, brand_token).

    spoof_suspected=True when the display name contains a known brand
    token AND the from-address domain is not legitimate for that brand.
    brand_token is the matched token (lowercased) so the audit log can
    record which brand triggered the flag.

    Returns (False, None) when:
    - No brand token is present in the display name.
    - The brand token is present and the from-address domain IS
      legitimate for that brand.
    - The display name is missing entirely (we can't make a claim about
      mismatch with no brand to compare).
    """
    name = (from_name or "").strip().lower()
    if not name:
        return (False, None)
    addr_domain = _domain_of(from_address)

    for token, legit_domains in BRAND_LEGIT_DOMAINS.items():
        # Word-boundary match. We treat the display name as a whitespace-
        # delimited string so multi-word tokens ("wells fargo", "bank of
        # america") match cleanly.
        padded = f" {name} "
        if f" {token} " not in padded and not name.startswith(token + " ") and not name.endswith(" " + token) and name != token:
            continue
        # "bank" is a generic; only flag when it co-occurs with a brand
        # we don't know AND no legit_domains entry — i.e., always under-
        # flag the bare token.
        if not legit_domains:
            continue
        if _matches_legit_domain(addr_domain, legit_domains):
            return (False, None)
        return (True, token)
    return (False, None)


def redact_task_body_for_financial_sender(
    *,
    from_name: str,
    subject: str,
) -> str:
    """Build a paraphrase-only task body for a financial-domain email.

    Spec: sender + subject summary + "balance available, see source email."
    No verbatim email body content reaches MS To Do; Megha goes to the
    source if she needs the numerics.
    """
    sender_label = (from_name or "").strip() or "financial sender"
    subject_label = (subject or "").strip() or "(no subject)"
    return (
        f"From: {sender_label[:80]}\n"
        f"Subject: {subject_label[:200]}\n\n"
        "Financial-domain email — body redacted by policy. "
        "See source email for the balance, account identifier, or numerics."
    )
