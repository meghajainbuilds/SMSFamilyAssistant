"""Tests for the extended sensitive-pattern set
(A6 of the 2026-05-06 evening audit follow-up).

Original set covered credit-card / SSN / routing / account-number numerics.
The audit recommended extending coverage to API-key prefixes (Anthropic,
Stripe, AWS, GitHub, Slack), bearer tokens, and 2FA-code shapes. Each new
pattern should raise OutboundContentBlocked when it appears in candidate
task body / iMessage text.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_extended_sensitive_patterns.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kavi_runtime.runtime import outbound_scanner


@pytest.fixture
def cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
        },
    }


def _read_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(cfg["paths"]["outbound_blocked_jsonl"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---- API-key prefixes ---------------------------------------------------


def test_anthropic_api_key_prefix_is_blocked(cfg: dict[str, Any]) -> None:
    text = "FYI my key is " + "sk-" + "ant-api01-AbCdEfGhIjKlMnOpQrStUvWx12345"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=text, surface="todo_body",
    )
    assert allowed is False
    assert "api_key_anthropic" in (reason or "")


def test_stripe_publishable_key_is_blocked(cfg: dict[str, Any]) -> None:
    text = "Stripe live key pk_live_AbCdEfGhIjKlMnOpQrStUvWx for the school portal"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=text, surface="todo_body",
    )
    assert allowed is False
    assert "api_key_stripe_pk" in (reason or "")


def test_aws_access_key_id_is_blocked(cfg: dict[str, Any]) -> None:
    text = "old AWS access key " + "AKIA" + "IOSFODNN7EXAMPLE in the email"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=text, surface="todo_body",
    )
    assert allowed is False
    assert "api_key_aws_access" in (reason or "")


def test_github_pat_is_blocked(cfg: dict[str, Any]) -> None:
    # 36 chars after ghp_
    text = "GitHub PAT " + "gh" + "p_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 leaked"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=text, surface="todo_body",
    )
    assert allowed is False
    assert "api_key_github_pat" in (reason or "")


def test_slack_bot_token_is_blocked(cfg: dict[str, Any]) -> None:
    # xoxb- + 40+ chars
    text = "Slack bot token " + "xox" + "b-1234567890-AbCdEfGhIjKlMnOpQrStUvWxYz123 leaked"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=text, surface="todo_body",
    )
    assert allowed is False
    assert "api_key_slack_bot" in (reason or "")


# ---- Bearer tokens -------------------------------------------------------


def test_bearer_token_in_body_is_blocked(cfg: dict[str, Any]) -> None:
    text = "Authorization Bearer eyJhbGciOiJIUzI1NiJ9.AbCd_eFg-HiJk-LmNo for the API"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=text, surface="todo_body",
    )
    assert allowed is False
    # Bearer pattern wins or another pattern wins on first match — the
    # ground truth is "the gate blocks", not which named pattern caught it.
    assert reason is not None and reason.startswith("sensitive_pattern_")


# ---- 2FA-code shapes -----------------------------------------------------


def test_two_factor_code_is_blocked(cfg: dict[str, Any]) -> None:
    text = "Your verification code is 482915 — expires in 10 minutes"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=text, surface="todo_body",
    )
    assert allowed is False
    # First-match-wins ordering — confirm a sensitive-pattern-shaped reason
    # so a future pattern reorder doesn't break this test.
    assert reason is not None and reason.startswith("sensitive_pattern_")
