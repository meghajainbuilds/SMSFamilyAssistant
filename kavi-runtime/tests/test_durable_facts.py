"""Tests for kavi_runtime.runtime.durable_facts (G-C3 v0 storage layer).

Covers:
- record_fact writes a row with valid fact_id and returns it.
- read_active_facts returns active rows; scope filter works.
- supersede_fact appends a superseded row; read_active_facts excludes it.
- expire_facts appends expired rows for past-due facts; read_active_facts excludes them.
- since filter works on a 2-row fixture.

All tests use a `tmp_path`-scoped jsonl file so the real
/Users/kavi/HomeOS/learned_facts.jsonl is never touched.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_durable_facts.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kavi_runtime.runtime import durable_facts


@pytest.fixture
def tmp_facts_config(tmp_path: Path) -> dict:
    """Config dict pointing at a tmp_path-scoped learned_facts.jsonl. Each
    test gets a fresh empty file."""
    return {"paths": {"learned_facts": str(tmp_path / "learned_facts.jsonl")}}


# ---- record_fact -----------------------------------------------------------


def test_record_fact_writes_row_and_returns_valid_fact_id(tmp_facts_config: dict) -> None:
    fid = durable_facts.record_fact(
        "Kavi committed to only message Megha when a task is actually marked done",
        scope="kavi",
        config=tmp_facts_config,
    )
    assert fid.startswith("f_")
    # Format: f_<iso ts>_<8 hex>
    parts = fid.split("_")
    assert len(parts) == 3
    assert len(parts[2]) == 8  # uuid hex slice

    path = Path(tmp_facts_config["paths"]["learned_facts"])
    assert path.exists()
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["fact_id"] == fid
    assert row["scope"] == "kavi"
    assert row["status"] == "active"
    assert row["source_decision_id"] is None
    assert row["expires_at"] is None
    assert "Kavi committed" in row["fact_text"]
    assert row["ts"].endswith("Z")


def test_record_fact_rejects_invalid_scope(tmp_facts_config: dict) -> None:
    with pytest.raises(ValueError, match="invalid scope"):
        durable_facts.record_fact(
            "x", scope="not-a-scope", config=tmp_facts_config
        )


def test_record_fact_rejects_empty_text(tmp_facts_config: dict) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        durable_facts.record_fact("   ", scope="megha", config=tmp_facts_config)


def test_record_fact_falls_back_to_default_path_when_config_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When config lacks paths.learned_facts, the module falls back to the
    hard-coded default. We monkey-patch the default to avoid touching the
    real /Users/kavi/HomeOS/learned_facts.jsonl during tests."""
    fake_default = tmp_path / "fallback_default.jsonl"
    monkeypatch.setattr(durable_facts, "_DEFAULT_PATH", fake_default)

    fid = durable_facts.record_fact("fallback test", scope="megha", config={})
    assert fid.startswith("f_")
    assert fake_default.exists()


# ---- read_active_facts -----------------------------------------------------


def test_read_active_facts_returns_recorded_row(tmp_facts_config: dict) -> None:
    fid = durable_facts.record_fact(
        "Max is out of town this week, route everything to Megha",
        scope="household",
        config=tmp_facts_config,
    )
    facts = durable_facts.read_active_facts(config=tmp_facts_config)
    assert len(facts) == 1
    assert facts[0]["fact_id"] == fid
    assert facts[0]["status"] == "active"


def test_read_active_facts_scope_filter(tmp_facts_config: dict) -> None:
    f_megha = durable_facts.record_fact(
        "Megha prefers no pings during 9-11 deep work blocks",
        scope="megha",
        config=tmp_facts_config,
    )
    f_max = durable_facts.record_fact(
        "Max handles all package pickups",
        scope="max",
        config=tmp_facts_config,
    )
    f_house = durable_facts.record_fact(
        "Quiet hours are 23:00-07:00 PT",
        scope="household",
        config=tmp_facts_config,
    )

    megha_only = durable_facts.read_active_facts(scope="megha", config=tmp_facts_config)
    assert {r["fact_id"] for r in megha_only} == {f_megha}

    max_only = durable_facts.read_active_facts(scope="max", config=tmp_facts_config)
    assert {r["fact_id"] for r in max_only} == {f_max}

    all_facts = durable_facts.read_active_facts(config=tmp_facts_config)
    assert {r["fact_id"] for r in all_facts} == {f_megha, f_max, f_house}


def test_read_active_facts_returns_empty_when_file_missing(tmp_facts_config: dict) -> None:
    # File hasn't been touched yet.
    facts = durable_facts.read_active_facts(config=tmp_facts_config)
    assert facts == []


# ---- supersede_fact --------------------------------------------------------


def test_supersede_fact_writes_followup_row_and_excludes_from_read(
    tmp_facts_config: dict,
) -> None:
    fid = durable_facts.record_fact(
        "Max is out of town this week",
        scope="household",
        config=tmp_facts_config,
    )
    # Sanity: active before supersede.
    before = durable_facts.read_active_facts(config=tmp_facts_config)
    assert {r["fact_id"] for r in before} == {fid}

    durable_facts.supersede_fact(fid, config=tmp_facts_config)

    # Raw file should now have 2 rows: the original active + a superseded row.
    path = Path(tmp_facts_config["paths"]["learned_facts"])
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(rows) == 2
    statuses = [r["status"] for r in rows]
    assert "active" in statuses and "superseded" in statuses
    superseded_row = next(r for r in rows if r["status"] == "superseded")
    assert superseded_row["source_decision_id"] == fid

    # Read API: superseded fact is no longer surfaced.
    after = durable_facts.read_active_facts(config=tmp_facts_config)
    assert after == []


def test_supersede_fact_unknown_id_is_noop(tmp_facts_config: dict) -> None:
    # Should not raise, should not write anything.
    durable_facts.supersede_fact("f_2099-01-01T00:00:00Z_deadbeef", config=tmp_facts_config)
    path = Path(tmp_facts_config["paths"]["learned_facts"])
    assert not path.exists() or path.read_text() == ""


def test_supersede_fact_double_call_is_idempotent(tmp_facts_config: dict) -> None:
    fid = durable_facts.record_fact("test", scope="kavi", config=tmp_facts_config)
    durable_facts.supersede_fact(fid, config=tmp_facts_config)
    durable_facts.supersede_fact(fid, config=tmp_facts_config)
    path = Path(tmp_facts_config["paths"]["learned_facts"])
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    # First call adds 1 superseded row; second call is a no-op.
    assert len(rows) == 2


# ---- expire_facts ----------------------------------------------------------


def test_expire_facts_marks_past_due_facts_and_excludes_from_read(
    tmp_facts_config: dict,
) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    f_past = durable_facts.record_fact(
        "Max is out of town this week",
        scope="household",
        expires_at=past,
        config=tmp_facts_config,
    )
    f_future = durable_facts.record_fact(
        "School pickup is at 3pm this fall",
        scope="household",
        expires_at=future,
        config=tmp_facts_config,
    )
    f_indef = durable_facts.record_fact(
        "Megha goes by Megha not Meg",
        scope="megha",
        config=tmp_facts_config,
    )

    swept = durable_facts.expire_facts(config=tmp_facts_config)
    assert swept == 1

    # Read API: only future + indefinite remain active.
    active = durable_facts.read_active_facts(config=tmp_facts_config)
    active_ids = {r["fact_id"] for r in active}
    assert f_past not in active_ids
    assert active_ids == {f_future, f_indef}

    # Raw file: there should be one expired row pointing at f_past.
    path = Path(tmp_facts_config["paths"]["learned_facts"])
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    expired_rows = [r for r in rows if r["status"] == "expired"]
    assert len(expired_rows) == 1
    assert expired_rows[0]["source_decision_id"] == f_past


def test_expire_facts_returns_zero_when_nothing_due(tmp_facts_config: dict) -> None:
    durable_facts.record_fact("indefinite", scope="kavi", config=tmp_facts_config)
    swept = durable_facts.expire_facts(config=tmp_facts_config)
    assert swept == 0


def test_expire_facts_is_idempotent(tmp_facts_config: dict) -> None:
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    durable_facts.record_fact("stale", scope="kavi", expires_at=past, config=tmp_facts_config)

    first = durable_facts.expire_facts(config=tmp_facts_config)
    second = durable_facts.expire_facts(config=tmp_facts_config)
    assert first == 1
    # Second call: the fact is already expired, so nothing left to sweep.
    assert second == 0


# ---- since filter ----------------------------------------------------------


def test_since_filter_excludes_rows_at_or_before_cutoff(tmp_facts_config: dict) -> None:
    """Two-row fixture: write fact A, capture cutoff, write fact B, query
    with since=cutoff. Only B should come back."""
    fid_a = durable_facts.record_fact(
        "fact A — recorded first",
        scope="household",
        config=tmp_facts_config,
    )

    # Use a cutoff slightly after A's ts. We can't trust second-resolution
    # equality here, so read A's actual ts and bump by 1ms via string surgery
    # is fragile; simpler: query with a cutoff equal to A's ts (since is
    # strictly-greater-than, so A is excluded).
    path = Path(tmp_facts_config["paths"]["learned_facts"])
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    a_ts = rows[0]["ts"]

    # Force B's timestamp to be strictly later: sleep 1.1s OR monkey-patch.
    # We monkey-patch _utc_now_iso to advance one second past A.
    a_dt = datetime.strptime(a_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    b_dt = a_dt + timedelta(seconds=2)
    b_ts = b_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    import unittest.mock as mock
    with mock.patch.object(durable_facts, "_utc_now_iso", return_value=b_ts):
        fid_b = durable_facts.record_fact(
            "fact B — recorded second",
            scope="household",
            config=tmp_facts_config,
        )

    # since = a_ts → strictly-greater filter excludes A, includes B.
    after_a = durable_facts.read_active_facts(since=a_ts, config=tmp_facts_config)
    assert {r["fact_id"] for r in after_a} == {fid_b}

    # since = b_ts → both excluded (B's ts is not strictly greater than itself).
    after_b = durable_facts.read_active_facts(since=b_ts, config=tmp_facts_config)
    assert after_b == []

    # No since filter → both returned.
    no_filter = durable_facts.read_active_facts(config=tmp_facts_config)
    assert {r["fact_id"] for r in no_filter} == {fid_a, fid_b}


# ---- combined scope + since ------------------------------------------------


def test_scope_and_since_filters_combine(tmp_facts_config: dict) -> None:
    fid_megha = durable_facts.record_fact(
        "early megha fact",
        scope="megha",
        config=tmp_facts_config,
    )
    path = Path(tmp_facts_config["paths"]["learned_facts"])
    cutoff = json.loads(path.read_text().splitlines()[0])["ts"]

    # Force a later ts for the next two rows.
    later = (datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
             + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    import unittest.mock as mock
    with mock.patch.object(durable_facts, "_utc_now_iso", return_value=later):
        fid_megha2 = durable_facts.record_fact(
            "later megha fact",
            scope="megha",
            config=tmp_facts_config,
        )
        fid_max = durable_facts.record_fact(
            "later max fact",
            scope="max",
            config=tmp_facts_config,
        )

    res = durable_facts.read_active_facts(
        scope="megha", since=cutoff, config=tmp_facts_config
    )
    assert {r["fact_id"] for r in res} == {fid_megha2}
