"""Deterministic Pacific calendar anchor in the inbox-to-task selection layer.

Covers the relative-date anchor added 2026-06-22: `_normalize_email` must
expose a `received_pt` field derived from the email's received timestamp,
converted to America/Los_Angeles, so the downstream compose prompt can
resolve "by Monday" / "due Friday" against the email's actual PT day.

No network. Pure function-level assertions.
"""

from capabilities.inbox_to_task.selection import _normalize_email, _received_pt_anchor


def _msg(received: str | None) -> dict:
    m = {
        "id": "AAMk-test",
        "subject": "Soccer signup",
        "from": {"emailAddress": {"name": "Camp", "address": "camp@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "megha@example.com"}}],
        "body": {"content": "Please sign up by Monday."},
    }
    if received is not None:
        m["receivedDateTime"] = received
    return m


def test_received_pt_resolves_correct_pacific_day():
    # 17:00 UTC on 2026-06-19 == 10:00 AM PDT, still Friday the 19th.
    out = _normalize_email(_msg("2026-06-19T17:00:00Z"))
    assert out["received_pt"] == "2026-06-19 (Friday) PT"
    # Existing field untouched.
    assert out["received"] == "2026-06-19T17:00:00Z"


def test_received_pt_crosses_midnight_back_a_day():
    # 03:00 UTC == 20:00 PDT the *previous* day (2026-06-19 Friday).
    out = _normalize_email(_msg("2026-06-20T03:00:00Z"))
    assert out["received_pt"] == "2026-06-19 (Friday) PT"


def test_missing_timestamp_yields_none():
    out = _normalize_email(_msg(None))
    assert out["received_pt"] is None


def test_unparseable_timestamp_yields_none():
    assert _received_pt_anchor("not-a-date") is None
    assert _received_pt_anchor("") is None
