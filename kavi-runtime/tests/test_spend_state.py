"""Tests for kavi_runtime.spend_state — the persisted running-total
Anthropic spend counter (2026-06-10 fix for the dead $0.00 spend readings).

Time is injected via the `now=` parameter (same pattern as
error_budget.compute_rollup) so day/month boundaries are deterministic.
All times are Pacific — the household convention.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kavi_runtime import spend_state
from kavi_runtime.spend_state import (
    PREVIOUS_MONTHS_KEPT,
    read_spend,
    record_spend,
    spend_state_path,
)

PT = ZoneInfo("America/Los_Angeles")


def _config(tmp_path: Path) -> dict:
    return {"paths": {"imessage_state": str(tmp_path / "imessage-state.json")}}


def _pt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=PT)


# ---- basic increment + read -------------------------------------------------


def test_record_spend_increments_today_and_month(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    now = _pt(2026, 6, 10)
    record_spend(cfg, 0.10, model="claude-sonnet-4-6", call_type="t", now=now)
    record_spend(cfg, 0.25, model="claude-sonnet-4-6", call_type="t", now=now)

    out = read_spend(cfg, now=now)
    assert out["today_usd"] == pytest.approx(0.35)
    assert out["month_usd"] == pytest.approx(0.35)


def test_record_spend_persists_across_fresh_module_load(tmp_path: Path) -> None:
    """A new process (simulated by reloading the module) reads the same
    running total — the counter is on disk, not in memory."""
    cfg = _config(tmp_path)
    now = _pt(2026, 6, 10)
    record_spend(cfg, 1.50, model="claude-sonnet-4-6", call_type="t", now=now)

    reloaded = importlib.reload(spend_state)
    try:
        out = reloaded.read_spend(cfg, now=now)
        assert out["today_usd"] == pytest.approx(1.50)
        assert out["month_usd"] == pytest.approx(1.50)
    finally:
        importlib.reload(spend_state)


def test_record_spend_writes_atomically_with_no_tmp_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every write goes through state_io.atomic_write_json (per-writer-unique
    tmp via tempfile.mkstemp) and leaves no .tmp files behind."""
    cfg = _config(tmp_path)
    now = _pt(2026, 6, 10)

    calls: list[Path] = []
    real_writer = spend_state.atomic_write_json

    def spy(path, data, **kwargs):
        calls.append(Path(path))
        return real_writer(path, data, **kwargs)

    monkeypatch.setattr(spend_state, "atomic_write_json", spy)
    record_spend(cfg, 0.10, model="claude-sonnet-4-6", call_type="t", now=now)
    record_spend(cfg, 0.10, model="claude-sonnet-4-6", call_type="t", now=now)

    assert len(calls) == 2
    assert all(p == spend_state_path(cfg) for p in calls)
    assert not list(tmp_path.glob("*.tmp")), "atomic write left a tmp file behind"


def test_record_spend_skips_non_positive_amounts(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    now = _pt(2026, 6, 10)
    record_spend(cfg, 0.0, model="m", call_type="t", now=now)
    record_spend(cfg, -1.0, model="m", call_type="t", now=now)
    assert not spend_state_path(cfg).exists()
    assert read_spend(cfg, now=now) == {"today_usd": 0.0, "month_usd": 0.0}


# ---- failure safety ----------------------------------------------------------


def test_record_spend_on_corrupt_file_logs_rebuilds_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt spend file: the write call must not raise (API call path
    unaffected), the corruption is logged + archived, and the file is
    rebuilt with the new spend."""
    cfg = _config(tmp_path)
    now = _pt(2026, 6, 10)
    path = spend_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json!!!")

    with caplog.at_level("CRITICAL"):
        record_spend(cfg, 0.42, model="claude-sonnet-4-6", call_type="t", now=now)

    # Logged by state_io.read_json_recover.
    assert any("corrupt" in r.message.lower() for r in caplog.records)
    # Corrupt original archived alongside.
    assert list(tmp_path.glob("anthropic_spend.json.corrupt-*"))
    # File rebuilt: counter cold-started with the new spend.
    out = read_spend(cfg, now=now)
    assert out["today_usd"] == pytest.approx(0.42)
    assert out["month_usd"] == pytest.approx(0.42)
    assert json.loads(path.read_text())["month_key"] == "2026-06"


def test_record_spend_never_raises_on_broken_config() -> None:
    """No paths key at all — the hook sits on the API call path and must
    swallow its own failures."""
    record_spend({}, 0.10, model="m", call_type="t")  # must not raise


def test_read_spend_never_raises_on_broken_config() -> None:
    assert read_spend({}) == {"today_usd": 0.0, "month_usd": 0.0}


# ---- month rollover ----------------------------------------------------------


def test_month_rollover_archives_previous_month_and_resets(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    may = _pt(2026, 5, 20)
    june = _pt(2026, 6, 1)

    record_spend(cfg, 14.20, model="claude-sonnet-4-6", call_type="t", now=may)
    record_spend(cfg, 0.30, model="claude-sonnet-4-6", call_type="t", now=june)

    data = json.loads(spend_state_path(cfg).read_text())
    assert data["month_key"] == "2026-06"
    assert data["month_usd"] == pytest.approx(0.30)
    assert data["previous_months"]["2026-05"] == pytest.approx(14.20)
    # Old month's days pruned.
    assert list(data["days"].keys()) == ["2026-06-01"]

    out = read_spend(cfg, now=june)
    assert out["month_usd"] == pytest.approx(0.30)
    assert out["today_usd"] == pytest.approx(0.30)


def test_previous_months_archive_is_bounded_at_twelve(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    # Record across 14 consecutive months: 2025-05 .. 2026-06.
    months = [(2025, m) for m in range(5, 13)] + [(2026, m) for m in range(1, 7)]
    for year, month in months:
        record_spend(cfg, 1.0, model="claude-sonnet-4-6", call_type="t",
                     now=_pt(year, month, 10))

    data = json.loads(spend_state_path(cfg).read_text())
    prev = data["previous_months"]
    # 13 months were archived; bound keeps only the newest 12.
    assert len(prev) == PREVIOUS_MONTHS_KEPT == 12
    assert "2025-05" not in prev  # oldest dropped
    assert prev["2026-05"] == pytest.approx(1.0)  # newest archived month kept


def test_read_spend_on_stale_month_file_reads_zero(tmp_path: Path) -> None:
    """A file last written in May reads $0.00 in June — the lazy rollover
    must not leak last month's total into this month's gauge."""
    cfg = _config(tmp_path)
    record_spend(cfg, 9.99, model="claude-sonnet-4-6", call_type="t",
                 now=_pt(2026, 5, 31))
    out = read_spend(cfg, now=_pt(2026, 6, 1))
    assert out == {"today_usd": 0.0, "month_usd": 0.0}


# ---- Pacific day rollover ----------------------------------------------------


def test_day_rollover_starts_new_pacific_day_entry(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    day1 = _pt(2026, 6, 9)
    day2 = _pt(2026, 6, 10)

    record_spend(cfg, 0.50, model="claude-sonnet-4-6", call_type="t", now=day1)
    record_spend(cfg, 0.20, model="claude-sonnet-4-6", call_type="t", now=day2)

    data = json.loads(spend_state_path(cfg).read_text())
    assert data["days"]["2026-06-09"] == pytest.approx(0.50)
    assert data["days"]["2026-06-10"] == pytest.approx(0.20)

    # today_usd reads the right Pacific day; month accumulates both.
    assert read_spend(cfg, now=day2)["today_usd"] == pytest.approx(0.20)
    assert read_spend(cfg, now=day1)["today_usd"] == pytest.approx(0.50)
    assert read_spend(cfg, now=day2)["month_usd"] == pytest.approx(0.70)


def test_day_boundary_is_pacific_not_utc(tmp_path: Path) -> None:
    """2026-06-10 03:00 UTC is still 2026-06-09 in Seattle (20:00 PDT).
    The counter must bucket it on the Pacific day."""
    cfg = _config(tmp_path)
    late_evening_utc = datetime(2026, 6, 10, 3, 0, tzinfo=ZoneInfo("UTC"))
    record_spend(cfg, 0.33, model="claude-sonnet-4-6", call_type="t",
                 now=late_evening_utc)

    data = json.loads(spend_state_path(cfg).read_text())
    assert "2026-06-09" in data["days"]
    assert "2026-06-10" not in data["days"]
