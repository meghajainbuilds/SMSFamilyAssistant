"""test_state_schemas.py — verifies Pydantic validation on state writes
catches shape regressions at the call site, not at the next reader.

Why: 2026-05-06 PM. Today's corrupt-state incident wasn't caught at write
time. The next reader exploded on a malformed JSON tail. P2.7 adds
optional schema validation to atomic_write_json — programmer errors and
shape regressions raise ValidationError at the writer, traceback points
at the call site.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kavi_runtime.state_io import atomic_write_json
from kavi_runtime.state_schemas import ImessageState, SubscriptionState


# ---- ImessageState ---------------------------------------------------------


def test_imessage_state_accepts_default_shape(tmp_path):
    target = tmp_path / "imessage-state.json"
    atomic_write_json(target, {"questions": [], "summary_queue": []},
                      schema=ImessageState)
    assert json.loads(target.read_text()) == {"questions": [], "summary_queue": []}


def test_imessage_state_accepts_full_shape(tmp_path):
    target = tmp_path / "imessage-state.json"
    state = {
        "questions": [{"q": "abc"}],
        "summary_queue": [],
        "last_send_at": "2026-05-06T18:14:31Z",
        "auto_runs_paused": False,
        "pending_alerts": [],
    }
    atomic_write_json(target, state, schema=ImessageState)
    assert json.loads(target.read_text()) == state


def test_imessage_state_allows_extra_fields(tmp_path):
    """New runtime versions can add fields without breaking older readers."""
    target = tmp_path / "imessage-state.json"
    state = {"questions": [], "future_field": "tolerated"}
    atomic_write_json(target, state, schema=ImessageState)
    assert "future_field" in json.loads(target.read_text())


def test_imessage_state_rejects_wrong_type(tmp_path):
    """questions must be a list, not a dict."""
    target = tmp_path / "imessage-state.json"
    with pytest.raises(ValidationError):
        atomic_write_json(target, {"questions": {"oops": "dict not list"}},
                          schema=ImessageState)
    assert not target.exists(), "file must not be touched on validation failure"


# ---- SubscriptionState -----------------------------------------------------


VALID_SUB = {
    "subscription_id": "abc-123",
    "client_state": "secret-token",
    "expiration_dt": "2026-05-08T23:24:00Z",
}


def test_subscription_state_accepts_minimum(tmp_path):
    target = tmp_path / "subscription.json"
    atomic_write_json(target, VALID_SUB, schema=SubscriptionState)
    assert json.loads(target.read_text())["subscription_id"] == "abc-123"


def test_subscription_state_rejects_missing_subscription_id(tmp_path):
    target = tmp_path / "subscription.json"
    bad = {"client_state": "x", "expiration_dt": "2026-05-08T00:00:00Z"}
    with pytest.raises(ValidationError) as exc_info:
        atomic_write_json(target, bad, schema=SubscriptionState)
    assert "subscription_id" in str(exc_info.value)
    assert not target.exists()


def test_subscription_state_rejects_missing_client_state(tmp_path):
    target = tmp_path / "subscription.json"
    bad = {"subscription_id": "x", "expiration_dt": "2026-05-08T00:00:00Z"}
    with pytest.raises(ValidationError) as exc_info:
        atomic_write_json(target, bad, schema=SubscriptionState)
    assert "client_state" in str(exc_info.value)


def test_subscription_state_rejects_missing_expiration_dt(tmp_path):
    target = tmp_path / "subscription.json"
    bad = {"subscription_id": "x", "client_state": "y"}
    with pytest.raises(ValidationError) as exc_info:
        atomic_write_json(target, bad, schema=SubscriptionState)
    assert "expiration_dt" in str(exc_info.value)


def test_subscription_state_allows_extra_metadata(tmp_path):
    """The runtime stamps `account`, `created_at`, etc. — extras must pass."""
    target = tmp_path / "subscription.json"
    state = {**VALID_SUB, "account": "megha@example.com",
             "created_at": "2026-04-30T05:37:30Z"}
    atomic_write_json(target, state, schema=SubscriptionState)
    persisted = json.loads(target.read_text())
    assert persisted["account"] == "megha@example.com"


# ---- atomic_write_json contract --------------------------------------------


def test_no_schema_means_no_validation(tmp_path):
    """Backward compat: callers that don't pass a schema get the
    pre-P2.7 behavior (no validation, anything serializable writes)."""
    target = tmp_path / "anything.json"
    atomic_write_json(target, {"completely": "arbitrary", "data": [1, 2, 3]})
    assert json.loads(target.read_text())["data"] == [1, 2, 3]
