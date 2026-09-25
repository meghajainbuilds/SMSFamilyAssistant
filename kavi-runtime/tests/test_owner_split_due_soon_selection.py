"""Selection-layer tests for the 2026-06-10 periodic_summary product changes.

Two new selectors in `capabilities/kavi_persona/selection.py`:

  * Per-person split — `_owner_of_title` / `_filter_tasks_by_owner` route
    each task to Megha or Max via the title's owner-abbreviation prefix
    ("MJ " / "MM ", per capabilities/inbox-to-task.md). Unprefixed legacy
    titles route to Megha (household when-in-doubt rule).

  * Deadline runway — `_select_due_soon_tasks` picks open tasks whose due
    date is within `DEADLINE_RUNWAY_DAYS` days or already past, with
    per-task days_until / overdue info. Null dueDateTime (most tasks) is
    skipped silently.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_owner_split_due_soon_selection.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from capabilities.kavi_persona.selection import (
    DEADLINE_RUNWAY_DAYS,
    _compute_rollup_counts,
    _filter_tasks_by_owner,
    _owner_of_title,
    _select_due_soon_tasks,
)


# ---- prefix-map sync lock ----------------------------------------------------


def test_owner_prefix_map_matches_dispatch_abbrevs() -> None:
    """selection.OWNER_PREFIX_TO_PERSON is a literal mirror of
    OWNER_ABBREV in kavi_runtime/runtime/imessage_dispatch.py (kept
    separate to avoid the handler-chain circular import). If either side
    changes, this test forces the other to follow."""
    import kavi_runtime.handlers  # noqa: F401  (settles handler import order)
    from kavi_runtime.runtime.imessage_dispatch import OWNER_ABBREV
    from capabilities.kavi_persona.selection import OWNER_PREFIX_TO_PERSON
    assert OWNER_PREFIX_TO_PERSON == {
        abbrev: person for person, abbrev in OWNER_ABBREV.items()
    }


# ---- _owner_of_title --------------------------------------------------------


def test_mm_prefix_routes_to_max() -> None:
    assert _owner_of_title("MM Leave cleaner cash") == "max"


def test_mj_prefix_routes_to_megha() -> None:
    assert _owner_of_title("MJ Book dentist appointment") == "megha"


def test_unprefixed_legacy_title_routes_to_megha() -> None:
    """Legacy tasks created before the abbreviation convention carry no
    prefix; the household when-in-doubt rule routes them to Megha."""
    assert _owner_of_title("Book dentist appointment") == "megha"


def test_low_confidence_prefix_is_stripped_before_owner_parse() -> None:
    """`[?] MM ...` (low-confidence flag + owner abbrev) still routes to
    Max."""
    assert _owner_of_title("[?] MM Confirm daycare check") == "max"
    assert _owner_of_title("[?] Confirm daycare check") == "megha"


def test_prefix_must_be_a_whole_token() -> None:
    """A title that merely STARTS with the letters (no separating space)
    is not a prefix match — 'MMs birthday' is not Max's."""
    assert _owner_of_title("MMs birthday party") == "megha"


def test_owner_of_title_handles_none_and_empty() -> None:
    assert _owner_of_title(None) == "megha"
    assert _owner_of_title("") == "megha"


# ---- _filter_tasks_by_owner -------------------------------------------------


def test_filter_splits_mixed_list_per_person() -> None:
    tasks = [
        {"title": "MJ Book dentist", "task_id": "t1"},
        {"title": "MM Leave cleaner cash", "task_id": "t2"},
        {"title": "Order diapers", "task_id": "t3"},  # unprefixed → Megha
    ]
    megha = _filter_tasks_by_owner(tasks, "megha")
    max_ = _filter_tasks_by_owner(tasks, "max")
    assert [t["task_id"] for t in megha] == ["t1", "t3"]
    assert [t["task_id"] for t in max_] == ["t2"]


def test_filter_passes_none_through_for_fail_open() -> None:
    """The fetch helpers return None on Graph errors (fail-open → counts
    fall back to 0). The owner filter must not turn None into [] or the
    fail-open contract silently changes meaning."""
    assert _filter_tasks_by_owner(None, "megha") is None
    assert _filter_tasks_by_owner(None, "max") is None


def test_filter_empty_list_stays_empty() -> None:
    assert _filter_tasks_by_owner([], "max") == []


def test_per_person_counts_split_correctly() -> None:
    """If 3 tasks were added today and 1 is Max's, Max's rollup count says
    1 and Megha's says 2 — never the household total."""
    now = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)
    today = "2026-06-10T18:00:00Z"
    open_tasks = [
        {"title": "MJ Book dentist", "createdDateTime": today},
        {"title": "Order diapers", "createdDateTime": today},  # legacy → Megha
        {"title": "MM Leave cleaner cash", "createdDateTime": today},
    ]
    megha_counts = _compute_rollup_counts(
        _filter_tasks_by_owner(open_tasks, "megha"), None, now=now,
    )
    max_counts = _compute_rollup_counts(
        _filter_tasks_by_owner(open_tasks, "max"), None, now=now,
    )
    assert megha_counts["tasks_added_today_count"] == 2
    assert max_counts["tasks_added_today_count"] == 1


# ---- _select_due_soon_tasks -------------------------------------------------

# Fixed "now": 2026-06-10 17:00 UTC == 10:00 PT, so today-Pacific is
# 2026-06-10 with no midnight-straddle ambiguity.
_NOW = datetime(2026, 6, 10, 17, 0, tzinfo=timezone.utc)


def _task(title: str, due: str | None):
    t: dict = {"title": title}
    if due is not None:
        t["dueDateTime"] = {"dateTime": f"{due}T00:00:00.0000000", "timeZone": "UTC"}
    return t


def test_due_within_runway_is_selected_with_days_until() -> None:
    out = _select_due_soon_tasks(
        [_task("MJ Pay Boonli invoice", "2026-06-11")], now=_NOW,
    )
    assert len(out) == 1
    assert out[0]["title"] == "MJ Pay Boonli invoice"
    assert out[0]["due_date"] == "2026-06-11"
    assert out[0]["days_until"] == 1


def test_due_today_is_selected() -> None:
    out = _select_due_soon_tasks([_task("MJ Pay rent", "2026-06-10")], now=_NOW)
    assert len(out) == 1
    assert out[0]["days_until"] == 0


def test_due_at_runway_edge_is_selected() -> None:
    """A task due exactly DEADLINE_RUNWAY_DAYS out enters the digest on
    the first morning of the runway."""
    edge = (_NOW + timedelta(days=DEADLINE_RUNWAY_DAYS)).date().isoformat()
    out = _select_due_soon_tasks([_task("MJ Renew passport", edge)], now=_NOW)
    assert len(out) == 1
    assert out[0]["days_until"] == DEADLINE_RUNWAY_DAYS


def test_due_beyond_runway_is_excluded() -> None:
    beyond = (_NOW + timedelta(days=DEADLINE_RUNWAY_DAYS + 1)).date().isoformat()
    out = _select_due_soon_tasks([_task("MJ Renew passport", beyond)], now=_NOW)
    assert out == []


def test_overdue_but_open_task_keeps_surfacing() -> None:
    """Overdue-and-still-open tasks stay in the runway every morning until
    closed — days_until goes negative, it never ages out."""
    out = _select_due_soon_tasks([_task("MJ Submit BCBA paperwork", "2026-06-08")], now=_NOW)
    assert len(out) == 1
    assert out[0]["days_until"] == -2


def test_null_due_date_is_skipped() -> None:
    """Most tasks carry no due date; they never enter the runway and never
    crash the selector."""
    out = _select_due_soon_tasks(
        [
            {"title": "MJ No due date at all"},
            {"title": "MJ Explicit null", "dueDateTime": None},
            _task("MJ Pay Boonli invoice", "2026-06-11"),
        ],
        now=_NOW,
    )
    assert [d["title"] for d in out] == ["MJ Pay Boonli invoice"]


def test_unparseable_due_date_is_skipped() -> None:
    out = _select_due_soon_tasks(
        [{"title": "MJ Garbage due", "dueDateTime": {"dateTime": "not-a-date"}}],
        now=_NOW,
    )
    assert out == []


def test_plain_string_due_date_is_tolerated() -> None:
    """Forward compat: a bare ISO string instead of Graph's wrapped dict."""
    out = _select_due_soon_tasks(
        [{"title": "MJ Pay rent", "dueDateTime": "2026-06-10T07:00:00Z"}],
        now=_NOW,
    )
    assert len(out) == 1
    assert out[0]["days_until"] == 0


def test_results_sorted_most_urgent_first() -> None:
    out = _select_due_soon_tasks(
        [
            _task("MJ Due tomorrow", "2026-06-11"),
            _task("MJ Overdue two days", "2026-06-08"),
            _task("MJ Due today", "2026-06-10"),
        ],
        now=_NOW,
    )
    assert [d["days_until"] for d in out] == [-2, 0, 1]


def test_none_and_empty_open_tasks_yield_empty_runway() -> None:
    assert _select_due_soon_tasks(None, now=_NOW) == []
    assert _select_due_soon_tasks([], now=_NOW) == []


def test_due_soon_items_carry_resolved_weekday() -> None:
    """2026-06-10 Verifier observation: the composer wrote "due Sunday" for
    a Monday date when left to derive the day name from the bare date.
    Selection resolves the weekday so the LLM never does calendar math."""
    out = _select_due_soon_tasks(
        [
            _task("MJ BCBA paperwork", "2026-06-08"),  # a Monday
            _task("MJ Boonli invoice", "2026-06-11"),  # a Thursday
        ],
        now=_NOW,
    )
    by_title = {d["title"]: d for d in out}
    assert by_title["MJ BCBA paperwork"]["due_weekday"] == "Monday"
    assert by_title["MJ Boonli invoice"]["due_weekday"] == "Thursday"
