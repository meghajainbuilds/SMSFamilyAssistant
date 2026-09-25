"""Validation tests for the SEED matrices shipped with the capability-build
pipeline (capabilities/BUILD_PIPELINE.md).

Asserts, against the real evals/ tree:

- each seed matrix parses as valid matrix JSONL (required keys, unique
  case_ids, legal verdicts) with at least the PM-agreed case count
  (kavi-persona >= 10, kavi-coordinates >= 4, inbox-to-task >= 4);
- each matrix matches its freeze manifest (`matrix_freeze.check` passes);
- every payload key is a key the corresponding /synthetic/verify route
  actually reads — derived by importing each route's verify function and
  extracting its `<payload>.get("...")` reads from source, so a schema
  drift in capabilities/<name>/verify.py fails here instead of silently
  no-opping a matrix case;
- expect blocks only use the documented fields.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from scripts import matrix_freeze

EVALS_ROOT = Path(__file__).resolve().parents[2] / "evals"

# The frozen matrices hold real household emails and messages, so they are
# private (gitignored). A public clone has none; skip rather than fail.
if not any(EVALS_ROOT.glob("*/matrix/matrix-*.jsonl")):
    pytest.skip("private eval matrices not present (public clone)", allow_module_level=True)

SEED_MINIMUMS = {
    "kavi-persona": 10,
    "kavi-coordinates": 4,
    "inbox-to-task": 4,
}

ALLOWED_EXPECT_KEYS = {"verdict", "must_contain_any", "must_not_contain"}


def _keys_read_by(func, var_name: str) -> set[str]:
    """Extract the literal keys `func` reads off `var_name` via .get()."""
    source = inspect.getsource(func)
    return set(re.findall(rf'{var_name}\.get\(\s*"(\w+)"', source))


def _persona_payload_keys() -> set[str]:
    from capabilities.kavi_persona import verify as persona_verify
    return _keys_read_by(
        persona_verify.replay_periodic_summary, "state_snapshot"
    ) | _keys_read_by(
        persona_verify.verify_periodic_summary_selection, "state_snapshot"
    )


def _coordination_payload_keys() -> set[str]:
    from capabilities.coordination import verify as coord_verify
    return _keys_read_by(coord_verify._compose_addressee, "session_payload")


def _inbox_payload_keys() -> set[str]:
    from capabilities.inbox_to_task import verify as inbox_verify
    return _keys_read_by(inbox_verify.verify_email_classify_selection, "body")


# Normalized email dict shape: the production replay fixture in
# tests/test_synthetic_verify_inbox_to_task.py and the endpoint docstring
# in capabilities/realtime_kavi/server.py (synthetic_compose_inbox_to_task).
EMAIL_PAYLOAD_KEYS = {
    "id", "subject", "from_name", "from_address", "to",
    "received", "body_text", "source_account",
}


@pytest.mark.parametrize("capability,minimum", sorted(SEED_MINIMUMS.items()))
def test_seed_matrix_parses_with_minimum_cases(capability: str, minimum: int) -> None:
    rows = matrix_freeze.load_matrix_rows(capability, EVALS_ROOT)
    assert len(rows) >= minimum, (
        f"{capability} seed matrix has {len(rows)} cases; PM-agreed minimum "
        f"is {minimum}"
    )


@pytest.mark.parametrize("capability", sorted(SEED_MINIMUMS))
def test_seed_matrix_is_frozen_and_unmodified(capability: str) -> None:
    ok, message = matrix_freeze.check(capability, EVALS_ROOT)
    assert ok, (
        f"seed matrix for {capability} fails the freeze check — either it "
        f"was never frozen or it drifted from its manifest. If the change "
        f"is legitimate, re-freeze explicitly (anti-Goodhart rule #1, "
        f"capabilities/BUILD_PIPELINE.md).\n{message}"
    )


@pytest.mark.parametrize("capability", sorted(SEED_MINIMUMS))
def test_seed_matrix_expect_blocks_use_documented_fields(capability: str) -> None:
    for row in matrix_freeze.load_matrix_rows(capability, EVALS_ROOT):
        extra = set(row["expect"]) - ALLOWED_EXPECT_KEYS
        assert not extra, (
            f"{capability}/{row['case_id']}: unknown expect fields {extra}"
        )


def test_persona_payload_keys_match_verify_route_schema() -> None:
    allowed = _persona_payload_keys()
    assert allowed, "could not extract payload keys from kavi_persona.verify"
    for row in matrix_freeze.load_matrix_rows("kavi-persona", EVALS_ROOT):
        extra = set(row["payload"]) - allowed
        assert not extra, (
            f"kavi-persona/{row['case_id']}: payload keys {sorted(extra)} are "
            f"not read by the /synthetic/verify/kavi-persona route "
            f"(capabilities/kavi_persona/verify.py reads {sorted(allowed)})"
        )


def test_coordination_payload_keys_match_verify_route_schema() -> None:
    allowed = _coordination_payload_keys()
    assert allowed, "could not extract payload keys from coordination.verify"
    for row in matrix_freeze.load_matrix_rows("kavi-coordinates", EVALS_ROOT):
        extra = set(row["payload"]) - allowed
        assert not extra, (
            f"kavi-coordinates/{row['case_id']}: payload keys {sorted(extra)} "
            f"are not read by the /synthetic/verify/kavi-coordinates route "
            f"(capabilities/coordination/verify.py reads {sorted(allowed)})"
        )


def test_inbox_payload_keys_match_verify_route_schema() -> None:
    allowed = _inbox_payload_keys()
    assert allowed, "could not extract payload keys from inbox_to_task.verify"
    for row in matrix_freeze.load_matrix_rows("inbox-to-task", EVALS_ROOT):
        payload = row["payload"]
        extra = set(payload) - allowed
        assert not extra, (
            f"inbox-to-task/{row['case_id']}: payload keys {sorted(extra)} "
            f"are not read by the /synthetic/verify/inbox-to-task route "
            f"(capabilities/inbox_to_task/verify.py reads {sorted(allowed)})"
        )
        email = payload.get("email")
        assert isinstance(email, dict), (
            f"inbox-to-task/{row['case_id']}: payload must carry an 'email' object"
        )
        extra_email = set(email) - EMAIL_PAYLOAD_KEYS
        assert not extra_email, (
            f"inbox-to-task/{row['case_id']}: email keys {sorted(extra_email)} "
            f"not in the normalized email shape {sorted(EMAIL_PAYLOAD_KEYS)}"
        )


def test_persona_seed_covers_the_pm_agreed_case_classes() -> None:
    """The 10 kavi-persona seed cases were scoped with the PM: morning
    all-clear, due-soon incl. overdue weekday, theme present, no-theme
    control, 9pm three-count, 9pm all-zero, close-suggestion single,
    close-suggestions overflow, owner-leak adversarial, pending-fact
    content. Locks the case_ids so a future trim is visible."""
    case_ids = {
        r["case_id"]
        for r in matrix_freeze.load_matrix_rows("kavi-persona", EVALS_ROOT)
    }
    required = {
        "morning-all-clear",
        "morning-due-soon-overdue-weekday",
        "morning-theme-present",
        "morning-no-theme-control",
        "rollup-9pm-three-count",
        "rollup-9pm-all-zero",
        "rollup-9pm-close-suggestion-single",
        "rollup-9pm-close-suggestions-overflow",
        "owner-leak-adversarial-mm-task-in-megha-payload",
        "morning-pending-fact-content",
    }
    missing = required - case_ids
    assert not missing, f"kavi-persona seed matrix missing case classes: {missing}"
