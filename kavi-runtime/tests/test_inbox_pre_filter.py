"""Tests for kavi_runtime.inbox_pre_filter (REC-1 from audits/
token_optimization_2026-05-06.md).

The pre-filter is the biggest single dollar lever in the audit (~$50-60/mo).
Today it ships in SHADOW MODE — matches log to runtime_metrics/
inbox_pre_filter_shadow.jsonl alongside the LLM's actual decision; the
LLM still runs. After 7 days of clean shadow data Megha promotes by
flipping config.inbox_pre_filter.shadow_mode to False.

These tests cover:
  1. Sender-domain denylist hits
  2. List-Unsubscribe header detection
  3. Auto-Submitted: auto-generated header detection
  4. A clean email (household member's coworker) passes through
  5. Empty / malformed payload defensively returns (False, None)
  6. Shadow log writes the expected JSON shape
"""

from __future__ import annotations

import json

from kavi_runtime.inbox_pre_filter import (
    SENDER_DOMAIN_DENYLIST,
    SHADOW_REASON_AUTO_SUBMITTED,
    SHADOW_REASON_DENYLIST,
    SHADOW_REASON_LIST_UNSUBSCRIBE,
    shadow_log_path,
    would_pre_filter_skip,
    write_shadow_log,
)


def test_sender_domain_in_denylist_skips() -> None:
    """A from_address in the denylist returns (True, denylist_sender_domain)."""
    payload = {
        "id": "msg1",
        "from_address": "promo@e.m.hannaandersson.com",
        "subject": "30% off everything",
        "internetMessageHeaders": [],
    }
    matched, reason = would_pre_filter_skip(payload)
    assert matched is True
    assert reason == SHADOW_REASON_DENYLIST


def test_subdomain_of_denylisted_domain_skips() -> None:
    """`bounces.eml.nordstrom.com` should match because `eml.nordstrom.com`
    is on the denylist (subdomain match)."""
    payload = {
        "id": "msg2",
        "from_address": "noreply@bounces.eml.nordstrom.com",
        "subject": "Your Nordstrom Anniversary preview",
        "internetMessageHeaders": [],
    }
    matched, reason = would_pre_filter_skip(payload)
    assert matched is True
    assert reason == SHADOW_REASON_DENYLIST


def test_list_unsubscribe_header_skips() -> None:
    """RFC 2369 List-Unsubscribe header signals bulk mailing list →
    returns (True, header_list_unsubscribe)."""
    payload = {
        "id": "msg3",
        "from_address": "newsletter@somerandomdomain.com",
        "subject": "Weekly update",
        "internetMessageHeaders": [
            {"name": "List-Unsubscribe", "value": "<mailto:unsub@somerandomdomain.com>"},
        ],
    }
    matched, reason = would_pre_filter_skip(payload)
    assert matched is True
    assert reason == SHADOW_REASON_LIST_UNSUBSCRIBE


def test_auto_submitted_header_skips() -> None:
    """RFC 3834 Auto-Submitted: auto-generated → returns
    (True, header_auto_submitted_auto_generated)."""
    payload = {
        "id": "msg4",
        "from_address": "system@reportbot.example.com",
        "subject": "Daily report",
        "internetMessageHeaders": [
            {"name": "Auto-Submitted", "value": "auto-generated"},
        ],
    }
    matched, reason = would_pre_filter_skip(payload)
    assert matched is True
    assert reason == SHADOW_REASON_AUTO_SUBMITTED


def test_clean_email_from_household_coworker_passes() -> None:
    """A plain email from a person Megha knows — no denylist hit, no
    bulk-mail headers — must NOT match the pre-filter."""
    payload = {
        "id": "msg5",
        "from_address": "alice@notable-startup.com",
        "subject": "Quick question about the role",
        "internetMessageHeaders": [
            {"name": "From", "value": "Alice Doe <alice@notable-startup.com>"},
            {"name": "Date", "value": "Tue, 06 May 2026 10:00:00 -0700"},
        ],
    }
    matched, reason = would_pre_filter_skip(payload)
    assert matched is False
    assert reason is None


def test_empty_payload_returns_false() -> None:
    """Defensive: a malformed / empty payload must NOT short-circuit a
    real email through the gate. Returns (False, None)."""
    assert would_pre_filter_skip({}) == (False, None)
    assert would_pre_filter_skip(None) == (False, None)  # type: ignore[arg-type]


def test_shadow_log_writes_expected_json_shape(tmp_path) -> None:
    """write_shadow_log appends one JSON row with the documented schema:
    {ts, email_id, sender, subject, reason, llm_decision, llm_reason}."""
    metrics_dir = tmp_path / "runtime_metrics"
    write_shadow_log(
        metrics_dir,
        email_id="msg-abc-123",
        sender="promo@e.m.hannaandersson.com",
        subject="30% off everything",
        reason=SHADOW_REASON_DENYLIST,
        llm_decision="skipped",
        llm_reason="marketing",
    )
    log_path = shadow_log_path(metrics_dir)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row.keys()) >= {
        "ts", "email_id", "sender", "subject", "reason",
        "llm_decision", "llm_reason",
    }
    assert row["email_id"] == "msg-abc-123"
    assert row["sender"] == "promo@e.m.hannaandersson.com"
    assert row["subject"] == "30% off everything"
    assert row["reason"] == SHADOW_REASON_DENYLIST
    assert row["llm_decision"] == "skipped"
    assert row["llm_reason"] == "marketing"


def test_denylist_seeded_from_audit_top_senders() -> None:
    """Sanity check: the audit's 7 high-volume marketing senders are in
    the seeded denylist. Catches an accidental delete during list maintenance."""
    seeded = {
        "e.m.hannaandersson.com",
        "mail.vogue.com",
        "eml.nordstrom.com",
        "linkedin.com",
        "parentmap.com",
        "morning7.theskimm.com",
        "emails.farfetch.com",
    }
    assert seeded.issubset(SENDER_DOMAIN_DENYLIST)


# ---- Keep list (approved by Megha 2026-09-24) ----
# Cases come from real shadow rows: emails the filter matched that the LLM
# judge turned into tasks (must be kept) and newsletters it skipped (must
# still be filtered). The keep block is read from the real config.yaml so
# a config edit that drops a protection fails here.

from pathlib import Path

import pytest
import yaml

from kavi_runtime.inbox_pre_filter import keep_rule

_RUNTIME = Path(__file__).resolve().parent.parent
_CONFIG = _RUNTIME / "config.yaml"
if not _CONFIG.exists():  # fresh public clone: real config is private
    _CONFIG = _RUNTIME / "config.example.yaml"
_KEEP = dict(yaml.safe_load(_CONFIG.read_text())["inbox_pre_filter"]["keep"])
# Household addresses and school/insurer domains are private config values;
# the test pins fictional stand-ins so it passes against any config.
_KEEP["sender_addresses"] = [
    "megha@example.com", "megha.alt@example.com", "max@example.com",
    "max.alt@example.com", "hit-reply@linkedin.com",
    "inmail-hit-reply@linkedin.com", "messaging-digest-noreply@linkedin.com",
    "messages-noreply@linkedin.com",
]
_KEEP["sender_domains"] = [
    "maplestreetschool.org", "mybrightwheel.com", "brightwheel.com",
    "evergreenhealthplan.com",
]


@pytest.mark.parametrize("sender,subject", [
    ("office@maplestreetschool.org", "{All School Email} Reminder: No School Tomorrow, Monday, May 11"),
    ("RRteachers@maplestreetschool.org", "{RR Email} New Post from the Youngest Level"),
    ("noreply@mybrightwheel.com", "Action Needed: re-enrollment"),
    ("eob@evergreenhealthplan.com", "Your Explanation of Benefits is ready"),
    ("mychart@mychart.northgatemedical.org", "You have a new message"),
    ("noreply@mychart.org", "Test results available"),
    ("megha.alt@example.com", "FW: camp registration"),
    ("hit-reply@linkedin.com", "Harper replied to your InMail"),
    ("messaging-digest-noreply@linkedin.com", "Jordan just messaged you"),
    ("morgan.ellis@gmail.com", "Re: {RR Social Event} Invitation to Maple Street RR Summer Birthday Party"),
    ("cs@freshcrate.com", "Freshcrate: Update - Mango READY for PICKUP"),
    ("cs@freshcrate.com", "Freshcrate: Update on Your Banganapalle Mango #271946"),
    ("cs@freshcrate.com", "Kesar Mangoes – Shipment Arriving June 3"),
    ("support@littleloomshop.com", "Your order is out for delivery."),
    ("nordstrom@eml.nordstrom.com", "Megha, an item from your order was canceled"),
    ("quickbooks@notification.intuit.com", "Invoice from Harbor Lane Child Care and Preschool (Tuition Pledge)"),
])
def test_keep_list_protects_real_task_emails(sender: str, subject: str) -> None:
    assert keep_rule({"from_address": sender, "subject": subject}, _KEEP) is not None


@pytest.mark.parametrize("sender,subject", [
    ("notifications-noreply@linkedin.com", "You have 1 new invitation"),  # Megha: filter out
    ("jobalerts-noreply@linkedin.com", "10 new jobs for Director of Product"),
    ("newsletter@substack.com", "The weekly roundup"),
    ("dan@tldrnewsletter.com", "TLDR AI 2026-09-24"),
    ("promo@mail.vogue.com", "The fall edit"),
    ("hello@everlane.com", "New arrivals are here"),
])
def test_keep_list_does_not_rescue_newsletters(sender: str, subject: str) -> None:
    assert keep_rule({"from_address": sender, "subject": subject}, _KEEP) is None


def test_keep_rule_without_config_keeps_nothing() -> None:
    assert keep_rule({"from_address": "office@maplestreetschool.org", "subject": "x"}, None) is None


def test_shadow_log_records_kept_by(tmp_path) -> None:
    write_shadow_log(tmp_path, email_id="m1", sender="office@maplestreetschool.org",
                     subject="s", reason=SHADOW_REASON_LIST_UNSUBSCRIBE,
                     llm_decision="task", kept_by="keep_sender_domain")
    row = json.loads(shadow_log_path(tmp_path).read_text().splitlines()[-1])
    assert row["kept_by"] == "keep_sender_domain"
