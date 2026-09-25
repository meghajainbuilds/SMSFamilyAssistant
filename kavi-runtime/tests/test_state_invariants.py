"""test_state_invariants.py — startup self-check verifies token + subscription
state for every configured account. Closes the 2026-05-06 silent-failure gap
where the runtime came up with empty subscriptions/ and /health stayed 200.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kavi_runtime import graph_client
from capabilities.realtime_kavi import state_invariants
from capabilities.realtime_kavi.state_invariants import (
    Violation,
    check_startup_invariants,
    log_startup_invariants,
    SUBSCRIPTION_EXPIRY_BUFFER_MINUTES,
)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect the per-account state directories to tmp so the test never
    reads or writes the real ~/.config/kavi state.

    Also stubs `check_legacy_state_paths` to return []. Otherwise, on hosts
    where /Users/kavi/HomeOS/.claude/imessage-state.json still exists
    (e.g. mid-migration on Kavi's Mac), the legacy-path check would inject
    extra WARNING violations and break the per-account assertions in this
    file. Per-account behavior is what these tests are about; legacy-path
    coverage lives in test_legacy_state_paths.py.
    """
    token_dir = tmp_path / "tokens"
    sub_dir = tmp_path / "subscriptions"
    token_dir.mkdir()
    sub_dir.mkdir()
    monkeypatch.setattr(graph_client, "TOKEN_CACHE_DIR", token_dir)
    monkeypatch.setattr(graph_client, "SUBSCRIPTION_STATE_DIR", sub_dir)
    monkeypatch.setattr(state_invariants, "check_legacy_state_paths", lambda: [])
    return token_dir, sub_dir


NOW = datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc)


def _stage_account(token_dir, sub_dir, account: str, *, expires: datetime | None) -> None:
    (token_dir / f"{account}.json").write_text("token-cache-blob")
    if expires is not None:
        (sub_dir / f"{account}.json").write_text(json.dumps({
            "subscription_id": f"sub-{account}",
            "client_state": "x",
            "expiration_dt": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }))


def test_no_violations_when_state_is_healthy(isolated_state):
    token_dir, sub_dir = isolated_state
    _stage_account(token_dir, sub_dir, "megha@example.com",
                   expires=NOW + timedelta(days=2))
    violations = check_startup_invariants(["megha@example.com"], now=NOW)
    assert violations == []


def test_missing_token_cache_is_critical(isolated_state):
    _, sub_dir = isolated_state
    # Stage a subscription but no token.
    (sub_dir / "megha@example.com.json").write_text(json.dumps({
        "subscription_id": "sub-x",
        "expiration_dt": "2026-05-08T00:00:00Z",
        "client_state": "x",
    }))
    violations = check_startup_invariants(["megha@example.com"], now=NOW)
    assert len(violations) == 1
    assert violations[0].severity == "CRITICAL"
    assert "token cache missing" in violations[0].message
    assert "add_account" in violations[0].fix_command


def test_missing_subscription_state_is_critical(isolated_state):
    token_dir, _ = isolated_state
    (token_dir / "megha@example.com.json").write_text("blob")
    violations = check_startup_invariants(["megha@example.com"], now=NOW)
    assert len(violations) == 1
    assert violations[0].severity == "CRITICAL"
    assert "subscription state missing" in violations[0].message


def test_corrupt_subscription_state_is_critical(isolated_state):
    token_dir, sub_dir = isolated_state
    (token_dir / "megha@example.com.json").write_text("blob")
    (sub_dir / "megha@example.com.json").write_text("{not valid json")
    violations = check_startup_invariants(["megha@example.com"], now=NOW)
    assert len(violations) == 1
    assert violations[0].severity == "CRITICAL"
    assert "unreadable" in violations[0].message


def test_subscription_missing_required_keys_is_critical(isolated_state):
    token_dir, sub_dir = isolated_state
    (token_dir / "megha@example.com.json").write_text("blob")
    (sub_dir / "megha@example.com.json").write_text(json.dumps({"subscription_id": "x"}))
    violations = check_startup_invariants(["megha@example.com"], now=NOW)
    assert len(violations) == 1
    assert violations[0].severity == "CRITICAL"
    assert "missing required keys" in violations[0].message


def test_expired_subscription_is_critical(isolated_state):
    token_dir, sub_dir = isolated_state
    _stage_account(token_dir, sub_dir, "megha@example.com",
                   expires=NOW - timedelta(hours=1))
    violations = check_startup_invariants(["megha@example.com"], now=NOW)
    assert len(violations) == 1
    assert violations[0].severity == "CRITICAL"
    assert "expired" in violations[0].message


def test_subscription_within_renewal_buffer_is_warning(isolated_state):
    token_dir, sub_dir = isolated_state
    _stage_account(token_dir, sub_dir, "megha@example.com",
                   expires=NOW + timedelta(minutes=SUBSCRIPTION_EXPIRY_BUFFER_MINUTES - 5))
    violations = check_startup_invariants(["megha@example.com"], now=NOW)
    assert len(violations) == 1
    assert violations[0].severity == "WARNING"


def test_multi_account_violations_are_independent(isolated_state):
    """Megha healthy; Max has neither token nor subscription. After 2026-05-07
    Issue 3, the no-token-AND-no-subscription state is WARNING (not yet
    onboarded), not CRITICAL — so /health stays 200."""
    token_dir, sub_dir = isolated_state
    _stage_account(token_dir, sub_dir, "megha@example.com",
                   expires=NOW + timedelta(days=2))
    violations = check_startup_invariants(
        ["megha@example.com", "max@example.com"], now=NOW,
    )
    assert len(violations) == 1
    assert violations[0].account == "max@example.com"
    assert violations[0].severity == "WARNING"
    assert "not yet onboarded" in violations[0].message


def test_partial_onboarding_is_critical(isolated_state):
    """Token exists but subscription doesn't (or vice versa) means onboarding
    started and got stuck partway. That IS a real outage — keep it CRITICAL
    so /health flips to 503 and the operator notices."""
    token_dir, sub_dir = isolated_state
    # Megha has a token but no subscription file.
    (token_dir / "megha@example.com.json").write_text("{}")
    violations = check_startup_invariants(
        ["megha@example.com"], now=NOW,
    )
    assert len(violations) == 1
    assert violations[0].account == "megha@example.com"
    assert violations[0].severity == "CRITICAL"
    assert "subscription state missing" in violations[0].message


def test_log_startup_invariants_logs_critical_at_critical(isolated_state, caplog):
    token_dir, sub_dir = isolated_state
    violations = [
        Violation(severity="CRITICAL", account="megha@example.com",
                  message="token cache missing", fix_command="run X"),
    ]
    with caplog.at_level("WARNING"):
        log_startup_invariants(violations)
    critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert any("token cache missing" in r.message for r in critical_records)
    assert any("run X" in r.message for r in critical_records)
