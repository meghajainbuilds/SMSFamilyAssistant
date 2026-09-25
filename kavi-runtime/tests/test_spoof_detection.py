"""Tests for spoofed-sender v0 detection
(Fix 2 of the 2026-05-06 audit follow-up).

A phishing-shaped email arrives with a brand/authority token in the
display name (`IRS Treasury`) but the from-address domain is NOT
legitimate for that brand (`accounts@irs-treasury.example.org`). The
detector returns (True, "irs"). Legitimate mail (display="IRS",
from="@irs.gov") returns (False, None). Unrelated mail (no brand
token) returns (False, None).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_spoof_detection.py -v
"""

from __future__ import annotations

import pytest

from kavi_runtime.financial_domains import detect_spoof


# ---- true positives (spoof suspected) -------------------------------------


def test_irs_treasury_display_with_lookalike_domain_flags() -> None:
    """Display 'IRS Treasury' from a non-irs.gov domain trips the detector."""
    suspected, brand = detect_spoof(
        from_name="IRS Treasury",
        from_address="accounts@irs-treasury.example.org",
    )
    assert suspected is True
    assert brand in {"irs", "treasury"}  # either token can fire first


def test_chase_display_with_unrelated_domain_flags() -> None:
    """Display 'Chase Bank' from a clearly-unrelated domain trips the detector."""
    suspected, brand = detect_spoof(
        from_name="Chase Bank Security",
        from_address="alerts@chase-secure-login.com",
    )
    assert suspected is True
    assert brand == "chase"


# ---- true negatives (legitimate sender) -----------------------------------


def test_irs_display_with_irs_gov_domain_passes() -> None:
    """Display 'IRS' from @irs.gov is the legitimate case — no flag."""
    suspected, brand = detect_spoof(
        from_name="IRS Notifications",
        from_address="noreply@irs.gov",
    )
    assert suspected is False
    assert brand is None


def test_chase_display_with_chase_com_subdomain_passes() -> None:
    """Display 'Chase' from a chase.com subdomain — no flag."""
    suspected, brand = detect_spoof(
        from_name="Chase Online",
        from_address="alerts@notification.chase.com",
    )
    assert suspected is False
    assert brand is None


# ---- unrelated (no brand token in display name) ---------------------------


def test_no_brand_token_in_display_name_passes() -> None:
    """A normal personal email — no brand token, no flag."""
    suspected, brand = detect_spoof(
        from_name="Kate Morrow",
        from_address="kate.morrow@gmail.com",
    )
    assert suspected is False
    assert brand is None


def test_school_newsletter_passes() -> None:
    """Subject-rich display name without brand keywords passes cleanly."""
    suspected, brand = detect_spoof(
        from_name="Bayview Elementary",
        from_address="newsletter@bayview-school.org",
    )
    assert suspected is False
    assert brand is None
