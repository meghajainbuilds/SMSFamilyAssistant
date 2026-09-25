"""Freshness filter + atomic GC for pending_facts.jsonl.

Why this exists: 2026-05-27. Kavi was re-surfacing 3-week-expired "facts to
confirm" twice daily in the 9 PM iMessage rollup. Root cause: the runtime
read pending_facts.jsonl without checking `expires_at`. The fix:

  - Read-time filter: durable_facts.read_active_pending_facts skips rows
    whose expires_at is in the past.
  - Write-time GC: durable_facts.prune_expired_pending_facts atomically
    rewrites the file with expired rows removed (uniquely-named tmp,
    fsync, os.replace — closes 2026-05-06 concurrent-writer pattern).

These tests assert both, plus that handlers._count_pending_facts and
handlers._read_pending_facts_for_summary route through the filter so the
expired row never reaches the LLM composer.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_pending_facts_freshness_filter.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kavi_runtime.runtime import durable_facts
from kavi_runtime import handlers


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def mixed_pending_config(tmp_path: Path) -> dict[str, Any]:
    """Fixture jsonl with three pending_confirmation rows: one fresh (no
    expiry), one fresh (future expiry), one expired (past expiry)."""
    now = datetime.now(timezone.utc)
    pending_path = tmp_path / "pending_facts.jsonl"
    rows = [
        {
            "pending_id": "p_fresh_no_expiry",
            "ts": _iso(now - timedelta(hours=1)),
            "fact_text": "fresh fact with no expiry",
            "scope": "household",
            "source_decision_id": None,
            "expires_at": None,
            "status": "pending_confirmation",
            "inbound_source": "imessage from Max",
        },
        {
            "pending_id": "p_fresh_future",
            "ts": _iso(now - timedelta(hours=2)),
            "fact_text": "fresh fact, expires tomorrow",
            "scope": "max",
            "source_decision_id": None,
            "expires_at": _iso(now + timedelta(days=1)),
            "status": "pending_confirmation",
            "inbound_source": "imessage from Megha",
        },
        {
            "pending_id": "p_expired",
            "ts": _iso(now - timedelta(days=21)),
            "fact_text": "stale fact from three weeks ago",
            "scope": "household",
            "source_decision_id": None,
            "expires_at": _iso(now - timedelta(days=14)),
            "status": "pending_confirmation",
            "inbound_source": "imessage from Max",
        },
    ]
    pending_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return {
        "paths": {
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(pending_path),
        },
    }


# ---- read-time filter ------------------------------------------------------


def test_read_active_pending_facts_excludes_expired_rows(
    mixed_pending_config: dict[str, Any],
) -> None:
    """The read helper must not return rows whose expires_at is in the past."""
    rows = durable_facts.read_active_pending_facts(config=mixed_pending_config)
    assert len(rows) == 2
    ids = {r["pending_id"] for r in rows}
    assert ids == {"p_fresh_no_expiry", "p_fresh_future"}
    assert "p_expired" not in ids


def test_read_active_pending_facts_returns_empty_when_file_missing(
    tmp_path: Path,
) -> None:
    cfg = {
        "paths": {
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(tmp_path / "does_not_exist.jsonl"),
        },
    }
    assert durable_facts.read_active_pending_facts(config=cfg) == []


def test_read_active_pending_facts_keeps_unparseable_expiry_as_fresh(
    tmp_path: Path,
) -> None:
    """A malformed expires_at field shouldn't silently vanish from Megha's
    review queue. Better to over-surface and let Megha decide."""
    pending_path = tmp_path / "pending_facts.jsonl"
    rows = [
        {
            "pending_id": "p_garbage_expiry",
            "ts": "2026-05-01T10:00:00Z",
            "fact_text": "garbage-expiry fact",
            "scope": "household",
            "source_decision_id": None,
            "expires_at": "not-a-date",
            "status": "pending_confirmation",
            "inbound_source": "manual",
        },
    ]
    pending_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    cfg = {"paths": {"pending_facts": str(pending_path)}}
    assert len(durable_facts.read_active_pending_facts(config=cfg)) == 1


# ---- write-time GC ---------------------------------------------------------


def test_prune_expired_removes_expired_rows_from_disk(
    mixed_pending_config: dict[str, Any],
) -> None:
    """prune_expired_pending_facts must atomically rewrite the file so the
    expired row no longer appears on disk after the call."""
    path = Path(mixed_pending_config["paths"]["pending_facts"])

    before = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(before) == 3

    count = durable_facts.prune_expired_pending_facts(config=mixed_pending_config)
    assert count == 1

    after = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(after) == 2
    ids_after = {r["pending_id"] for r in after}
    assert "p_expired" not in ids_after
    # Fresh rows survived untouched.
    assert ids_after == {"p_fresh_no_expiry", "p_fresh_future"}


def test_prune_is_noop_when_nothing_expired(tmp_path: Path) -> None:
    """When no rows are expired, the GC must NOT rewrite the file."""
    now = datetime.now(timezone.utc)
    pending_path = tmp_path / "pending_facts.jsonl"
    pending_path.write_text(json.dumps({
        "pending_id": "p1",
        "ts": _iso(now),
        "fact_text": "fresh",
        "scope": "household",
        "source_decision_id": None,
        "expires_at": None,
        "status": "pending_confirmation",
        "inbound_source": "manual",
    }) + "\n")
    cfg = {"paths": {"pending_facts": str(pending_path)}}

    count = durable_facts.prune_expired_pending_facts(config=cfg)
    assert count == 0
    # File still contains the one row.
    assert pending_path.read_text().count("\n") == 1


def test_prune_is_noop_when_file_missing(tmp_path: Path) -> None:
    cfg = {"paths": {"pending_facts": str(tmp_path / "missing.jsonl")}}
    assert durable_facts.prune_expired_pending_facts(config=cfg) == 0


def test_prune_routes_through_state_io_atomic_write(
    mixed_pending_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-05-06 silent-failure pattern came from a shared `.tmp`
    filename under concurrent writers. The prune helper must therefore
    route through state_io.atomic_write_bytes — the single sanctioned
    primitive that uses tempfile.mkstemp's per-writer-unique tmp names.
    Routing through one helper is enforced project-wide by
    test_state_io_parity; this test asserts the prune path actually
    exercises that helper end-to-end.
    """
    called: list[tuple[Any, bytes]] = []
    from kavi_runtime import state_io as _state_io
    real_fn = _state_io.atomic_write_bytes

    def spy(path: Any, payload: bytes) -> None:
        called.append((path, payload))
        real_fn(path, payload)

    monkeypatch.setattr(
        "kavi_runtime.runtime.durable_facts.atomic_write_bytes", spy,
    )
    durable_facts.prune_expired_pending_facts(config=mixed_pending_config)
    assert called, "prune should have called atomic_write_bytes once"
    # Payload contains exactly the kept (non-expired) rows.
    payload = called[-1][1].decode("utf-8")
    assert "p_fresh_no_expiry" in payload
    assert "p_fresh_future" in payload
    assert "p_expired" not in payload


# ---- handlers wiring -------------------------------------------------------


def test_count_pending_facts_filters_out_expired(
    mixed_pending_config: dict[str, Any],
) -> None:
    """The handlers count helper feeds periodic_summary; it must report
    only fresh rows so the digest doesn't claim "5 pending facts" when
    all 5 are past their TTL."""
    count = handlers._count_pending_facts(mixed_pending_config)
    assert count == 2


def test_read_pending_facts_for_summary_filters_out_expired(
    mixed_pending_config: dict[str, Any],
) -> None:
    """The composer-input reader must skip expired rows so the LLM never
    sees stale "facts to confirm" text."""
    out = handlers._read_pending_facts_for_summary(mixed_pending_config, top_n=10)
    # 2 fresh rows; expired excluded.
    assert len(out) == 2
    snippets = " ".join(r["snippet"].lower() for r in out)
    assert "stale" not in snippets
    assert "three weeks ago" not in snippets
