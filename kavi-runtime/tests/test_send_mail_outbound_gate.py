"""Tests for the outbound-content gate on `GraphClient.send_mail`
(A1 of the 2026-05-06 evening audit follow-up).

Mirrors the shape of test_todo_outbound_content_gate.py — verifies that:
- Clean subject + body proceed to the Graph POST.
- Card-shaped numerics in the body raise OutboundContentBlocked BEFORE
  the POST, and write a row to outbound_blocked.jsonl with surface=
  "outbound_email".

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_send_mail_outbound_gate.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kavi_runtime import graph_client


from kavi_runtime.runtime import outbound_scanner as _outbound_scanner

# Test-owned fictional household handles. Pinned here so these tests do not
# depend on the real handles the runtime allowlist is configured with.
_TEST_HOUSEHOLD_HANDLES = {
    "+15555550101", "+15555550102", "megha@example.com", "max@example.com",
}


@pytest.fixture(autouse=True)
def _test_household_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _outbound_scanner, "is_household_handle",
        lambda h: bool(h) and h.strip().lower() in _TEST_HOUSEHOLD_HANDLES,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "graph": {
            "subscription_lifetime_minutes": 4230,
            "subscription_renewal_buffer_minutes": 60,
            "mstodo_shared_list_id": "AQMkADAwTEST==",
        },
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
            "household_md": str(tmp_path / "household.md"),
        },
    }


def _make_client(cfg: dict[str, Any]) -> graph_client.GraphClient:
    with patch.object(
        graph_client, "discover_household_accounts",
        return_value=["megha@example.com"],
    ), patch.object(graph_client, "PublicClientApplication", MagicMock()):
        return graph_client.GraphClient(cfg)


def _read_blocked_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(cfg["paths"]["outbound_blocked_jsonl"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_send_mail_passes_clean_content(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean subject + body proceeds through the gate to the Graph POST."""
    g = _make_client(cfg)

    monkeypatch.setattr(
        graph_client.GraphClient, "_access_token", lambda self, account: "fake-token",
    )

    fake_post = MagicMock()
    fake_post.return_value = MagicMock(
        status_code=202,
        json=lambda: {},
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(graph_client.httpx, "post", fake_post)

    g.send_mail(
        to="megha@example.com",
        subject="Heads up",
        body="Field trip on Friday — bring the permission slip.",
    )
    assert _read_blocked_rows(cfg) == []
    fake_post.assert_called_once()


def test_send_mail_blocks_credit_card_in_body(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 16-digit card-shaped numeric in the body trips credit_card and
    raises OutboundContentBlocked BEFORE the Graph POST."""
    g = _make_client(cfg)

    monkeypatch.setattr(
        graph_client.GraphClient, "_access_token", lambda self, account: "fake-token",
    )

    posted: list[Any] = []

    def _trap_post(*args, **kwargs):
        posted.append((args, kwargs))
        raise AssertionError("send_mail should NOT POST when the gate blocks")

    monkeypatch.setattr(graph_client.httpx, "post", _trap_post)

    with pytest.raises(graph_client.OutboundContentBlocked):
        g.send_mail(
            to="megha@example.com",
            subject="Receipt forward",
            body="Charged 4111 1111 1111 1111 today on the joint card.",
        )

    assert posted == []
    rows = _read_blocked_rows(cfg)
    assert any(r.get("surface") == "outbound_email" for r in rows)
    assert any("credit_card" in r.get("reason", "") for r in rows)


def test_send_mail_bypass_scanner_household_skips_gate(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 2 of 2026-05-07: alert emails to a household recipient skip
    the outbound scanner. A body that would normally trigger the
    account-number regex (10-12 digit standalone) MUST go through to
    Graph when bypass_scanner=True. This is the unlock that lets Kavi
    actually tell Megha when something breaks (Python error messages
    routinely contain digit sequences that the generic regex would
    block, leaving the operator with no heads-up email)."""
    g = _make_client(cfg)

    monkeypatch.setattr(
        graph_client.GraphClient, "_access_token", lambda self, account: "fake-token",
    )

    fake_post = MagicMock()
    fake_post.return_value = MagicMock(
        status_code=202,
        json=lambda: {},
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(graph_client.httpx, "post", fake_post)

    # Body contains a 10-digit run that would normally trip account_number.
    g.send_mail(
        to="megha@example.com",
        subject="Kavi couldn't reply to your 7:11am iMessage",
        body="Last error: NameError at line 2586123456 (10-digit run here).",
        bypass_scanner=True,
    )
    fake_post.assert_called_once()
    assert _read_blocked_rows(cfg) == []


def test_send_mail_bypass_scanner_non_household_raises(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bypass_scanner=True is reserved for runtime-internal alert emails
    sent back to a household member. Any other recipient must NOT be
    able to bypass the scanner. Defense in depth in case a future caller
    misuses the flag."""
    g = _make_client(cfg)

    monkeypatch.setattr(
        graph_client.GraphClient, "_access_token", lambda self, account: "fake-token",
    )

    posted: list[Any] = []

    def _trap_post(*args, **kwargs):
        posted.append((args, kwargs))
        raise AssertionError(
            "send_mail must NOT POST when bypass_scanner is used with a "
            "non-household recipient"
        )

    monkeypatch.setattr(graph_client.httpx, "post", _trap_post)

    with pytest.raises(graph_client.OutboundContentBlocked):
        g.send_mail(
            to="someone-external@example.com",
            subject="Whatever",
            body="Even clean content shouldn't bypass the gate to outsiders.",
            bypass_scanner=True,
        )
    assert posted == []
