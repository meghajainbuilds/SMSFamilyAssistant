"""kavi-persona selection layer — which inputs reach each composer.

Phase 4 (2026-06-02) physical move. Function bodies for the periodic_summary
input filters live here.

Selection for periodic_summary has these filter / read functions:

- `_filter_summary_queue_by_open_status` — drops queued-task items whose
  underlying MS To Do task is closed (caught the 2026-05-30 walk-for-kids
  bug for the summary_queue half).
- `_filter_pending_questions_by_open_status` — drops pending Q&As whose
  underlying MS To Do task is closed.
- `_fetch_open_todo_task_ids` — shared Graph snapshot the two filters use.
- `_fetch_open_todo_tasks` — full open task objects (with timestamps) for
  the rollup count fields. Phase 1 2026-06-02.
- `_fetch_completed_todo_tasks` — completed task objects for the rollup's
  tasks_completed_today_count. Phase 1 2026-06-02.
- `_compute_rollup_counts` — three counts for the 9 PM rollup shape:
  tasks_added_today, tasks_completed_today, tasks_over_7d. Phase 1 2026-06-02.
- `_pick_top_importance_tasks` — top 1-2 named items by is_priority flag
  (Megha's priority_senders list). Phase 1 2026-06-02.
- `_count_pending_facts` / `_read_pending_facts_for_summary` — pending
  durable-facts surfacing with freshness gate.
- `_pick_summary_anchor` — picks the anchor task to lead the rollup.
- `_owner_of_title` / `_filter_tasks_by_owner` — per-person split
  (2026-06-10): route each task to Megha or Max by the owner abbreviation
  prefix on the task title ("MJ " / "MM ", per the inbox-to-task title
  convention). Unprefixed legacy titles route to Megha (household
  when-in-doubt-route-to-Megha rule).
- `_select_due_soon_tasks` — deadline runway (2026-06-10): open tasks
  whose due date falls within `DEADLINE_RUNWAY_DAYS` days, or is already
  past, surface in the morning digest every day until the task is closed.
  No repeat-surfacing state needed; the list derives from open+due each
  morning.
- `_select_morning_theme` — top-of-mind theme (2026-06-10): when the
  recipient's open tasks cluster around one effort (>= `THEME_MIN_CLUSTER`
  supporting tasks), an LLM clustering judgment names the theme for the
  morning digest. Zero themes is the common case. Result is cached per
  recipient per Pacific day in the per-concept state file
  `morning_theme.json`; input is capped at `THEME_MAX_INPUT_TITLES`
  titles truncated to `THEME_TITLE_TRUNCATE_CHARS` chars.
- `_select_stale_task_nudge` — stale-task nudge (2026-06-11): on
  no-theme mornings (the common case), the recipient's 1-2 longest-open
  tasks (open strictly longer than `STALE_TASK_MIN_AGE_DAYS` days by
  `createdDateTime`, Pacific calendar days) surface as a
  close-out-old-threads frame. Cluster theme present → it wins; the
  caller never computes both (one top-of-mind frame per morning).

Evening suggest-to-close selection lives in the sibling module
`capabilities/kavi_persona/close_suggestions.py` (its own per-concept
state + cost caps).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from kavi_runtime.runtime import clients as _clients_module

logger = logging.getLogger(__name__)

_PACIFIC = ZoneInfo("America/Los_Angeles")

# Deadline runway (2026-06-10): a task with a due date enters the morning
# digest this many days before the due date and stays there every morning
# (including once overdue) until the task is closed. SELECTION-axis
# constant — it decides which inputs reach the composer, so its canonical
# home is this module (mirror of how structural caps live in
# `kavi_runtime/structural_checks.py`). The skill at
# `kavi-runtime/skills/periodic_summary_composer.md` references this
# constant by name and never inlines the number.
DEADLINE_RUNWAY_DAYS = 2

# Due-date reminders on the 9 PM rollup (2026-06-22, Megha). A task with a
# due date gets a day-BEFORE nudge (due tomorrow) and a day-OF follow-up if
# still open (due today), surfaced in that evening's 9 PM run. A task created
# the same day it is due folds into that evening's run too. Runway = 1 so the
# selection picks up tasks due tomorrow (days_until == 1), due today
# (days_until == 0), and already-overdue-but-open (days_until < 0) — the last
# being the natural extension of "follow up the same day if still open".
# SELECTION-axis constant; the skill references it by name, never the literal.
DUE_REMINDER_RUNWAY_DAYS = 1

# Coordination intent facts are recorded with a fixed audit-trail preamble
# ("Megha asked Kavi to coordinate with Max on: <ask>") by
# capabilities/coordination/handler.py. That preamble is bookkeeping, not
# content — under the old 60-char snippet cap it consumed ~45 of the 60
# chars and the digest composer received almost no actual content (the
# 2026-06-03 "coordinating with Max on something" week-long digest bug).
# Strip it at selection time so the LLM sees the ask itself.
_COORDINATION_FACT_PREFIX_RE = re.compile(
    r"^\s*\w+\s+asked\s+Kavi\s+to\s+coordinate\s+with\s+\w+\s+on:\s*",
    re.IGNORECASE,
)


def _fetch_open_todo_task_ids(config: dict) -> set[str] | None:
    """Return the set of MS To Do task_ids currently open in the McMullen-Jain
    Shared list. Returns None on any error or misconfig — callers treat None
    as "fail open, surface the original input" so Kavi doesn't go silent on
    the rollup when Graph is unreachable.
    """
    list_id = (config.get("graph") or {}).get("mstodo_shared_list_id")
    if not list_id:
        return None
    try:
        graph, _, _ = _clients_module._get_clients(config)
        open_tasks = graph.list_open_todo_tasks(list_id, top=200)
        if not isinstance(open_tasks, list):
            logger.warning(
                "periodic_summary: list_open_todo_tasks returned %s, "
                "not a list; treating as fail-open",
                type(open_tasks).__name__,
            )
            return None
        return {t.get("id") for t in open_tasks if t.get("id")}
    except Exception as e:
        logger.warning(
            "periodic_summary: open-status fetch failed (fail-open): %s", e,
        )
        return None


def _filter_summary_queue_by_open_status(
    config: dict,
    queued: list[dict[str, Any]],
    *,
    open_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Drop queue entries whose task has been marked done in MS To Do since
    enqueue."""
    if not queued:
        return queued
    if open_ids is None:
        open_ids = _fetch_open_todo_task_ids(config)
    if open_ids is None:
        return queued
    filtered = [q for q in queued if q.get("task_id") in open_ids]
    skipped = len(queued) - len(filtered)
    if skipped:
        logger.info(
            "periodic_summary: skipped %d done task(s) from queue", skipped,
        )
    return filtered


def _filter_pending_questions_by_open_status(
    config: dict,
    pending: list[dict[str, Any]],
    *,
    open_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Drop pending_question entries whose underlying MS To Do task has been
    marked done since the Q&A was created."""
    if not pending:
        return pending
    if open_ids is None:
        open_ids = _fetch_open_todo_task_ids(config)
    if open_ids is None:
        return pending
    filtered = [
        q for q in pending
        if not q.get("task_id") or q.get("task_id") in open_ids
    ]
    skipped = len(pending) - len(filtered)
    if skipped:
        logger.info(
            "periodic_summary: skipped %d done task(s) from pending_questions",
            skipped,
        )
    return filtered


def _fact_matches_recipient(scope: str | None, recipient: str | None) -> bool:
    """Scope-aware pending-fact routing (2026-06-23, Megha). A fact tagged
    `scope:"max"` (e.g. a coordination commitment BY Max) must NOT surface in
    Megha's digest and vice-versa — it routes to the person it's about, plus
    `household`/unset facts go to everyone. Fixes the Premier-Mechanical leak:
    the per-person digest split (2026-06-10) added owner filtering for tasks
    but left pending facts on the pre-split 'all facts are Megha's' assumption,
    so a Max-scoped coordination fact bled into Megha's 9 PM summary.

    recipient None = no filtering (legacy callers / counts of everything)."""
    if not recipient:
        return True
    s = (scope or "").strip().lower()
    if s in ("", "household"):
        return True  # unscoped/legacy + household facts go to everyone
    return s == recipient.strip().lower()


def _count_pending_facts(config: dict, recipient: str | None = None) -> int:
    """Count fresh pending facts awaiting confirmation, optionally scoped to
    `recipient` (see _fact_matches_recipient)."""
    try:
        from kavi_runtime.runtime import durable_facts
        rows = durable_facts.read_active_pending_facts(config=config)
        if recipient:
            rows = [r for r in rows if _fact_matches_recipient(r.get("scope"), recipient)]
        return len(rows)
    except Exception as e:
        logger.warning("periodic_summary: pending-facts count failed (continuing): %s", e)
        return 0


def _read_pending_facts_for_summary(
    config: dict, top_n: int = 3, recipient: str | None = None,
) -> list[dict[str, Any]]:
    """Read the top-N fresh pending facts (newest first) for composer
    consumption (periodic_summary and the conversational reply path),
    optionally scoped to `recipient` (see _fact_matches_recipient — a
    Max-scoped fact never reaches Megha's digest). Returns each as
    `{topic, snippet}`.

    Snippet carries the FULL fact text (2026-06-10): selection must not
    pre-compress what the LLM sees. The old `text[:60]` cap starved the
    digest composer — coordination facts open with ~45 chars of audit
    preamble, so the composer received almost no content and shipped
    "coordinating with Max on something" for a week. The composer already
    honors the outbound length cap (structural_checks); compression is
    its job, not selection's. The boilerplate preamble is stripped here
    for the same reason.
    """
    try:
        from kavi_runtime.runtime import durable_facts
        rows = durable_facts.read_active_pending_facts(config=config)
        if recipient:
            rows = [r for r in rows if _fact_matches_recipient(r.get("scope"), recipient)]
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        out: list[dict[str, Any]] = []
        for row in rows[:top_n]:
            text = (row.get("fact_text") or "").strip()
            text = _COORDINATION_FACT_PREFIX_RE.sub("", text).strip()
            source = (row.get("inbound_source") or "").strip()
            scope = (row.get("scope") or "").strip()
            topic = ""
            if source:
                low = source.lower()
                if " from " in low:
                    topic = source.split("from", 1)[1].strip().split()[0]
                else:
                    topic = source.split()[0] if source.split() else ""
            if not topic:
                topic = scope or "household"
            out.append({"topic": topic[:30], "snippet": text})
        return out
    except Exception as e:
        logger.warning(
            "periodic_summary: pending-facts read for surfacing failed: %s", e,
        )
        return []


def _fetch_open_todo_tasks(config: dict) -> list[dict[str, Any]] | None:
    """Return full open task objects from MS Graph (not just IDs).

    Used by the 9 PM rollup count computation. Returns None on misconfig or
    error; caller treats None as fail-open (the count fields fall back to
    0 and the composer narrates "0 added, 0 done, 0 over a week", which is
    honest under uncertainty per kavi-persona honesty rule).
    """
    list_id = (config.get("graph") or {}).get("mstodo_shared_list_id")
    if not list_id:
        return None
    try:
        graph, _, _ = _clients_module._get_clients(config)
        open_tasks = graph.list_open_todo_tasks(list_id, top=200)
        if not isinstance(open_tasks, list):
            logger.warning(
                "periodic_summary: list_open_todo_tasks returned %s, "
                "not a list; treating as fail-open (counts will be 0)",
                type(open_tasks).__name__,
            )
            return None
        return open_tasks
    except Exception as e:
        logger.warning(
            "periodic_summary: open task fetch failed (fail-open): %s", e,
        )
        return None


def _fetch_completed_todo_tasks(config: dict) -> list[dict[str, Any]] | None:
    """Return completed task objects from MS Graph for the 9 PM rollup.

    Used to compute tasks_completed_today_count. Returns None on misconfig
    or error; caller treats None as fail-open (count falls back to 0).
    """
    list_id = (config.get("graph") or {}).get("mstodo_shared_list_id")
    if not list_id:
        return None
    try:
        graph, _, _ = _clients_module._get_clients(config)
        completed = graph.list_completed_todo_tasks(list_id, top=100)
        if not isinstance(completed, list):
            logger.warning(
                "periodic_summary: list_completed_todo_tasks returned %s, "
                "not a list; treating as fail-open",
                type(completed).__name__,
            )
            return None
        return completed
    except Exception as e:
        logger.warning(
            "periodic_summary: completed task fetch failed (fail-open): %s", e,
        )
        return None


def _today_midnight_pacific_utc(now: datetime | None = None) -> datetime:
    """Return today's midnight Pacific Time as a UTC-aware datetime.

    'Today' is what Megha lives in (Seattle, America/Los_Angeles), not what
    the runtime's UTC clock reports. The 9 PM rollup runs at 21:00 Pacific
    and the 'tasks added today' count needs to mean 'since midnight Megha-
    time,' not 'since midnight UTC' (which would clip to the wrong day for
    8 hours).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    pacific_now = now.astimezone(_PACIFIC)
    pacific_midnight = pacific_now.replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return pacific_midnight.astimezone(timezone.utc)


def _parse_graph_iso(ts: str | None) -> datetime | None:
    """Parse an MS Graph ISO timestamp into a UTC-aware datetime.

    Returns None on any parse failure; caller treats None as 'unknown,'
    not 'old' or 'recent.'
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
            timezone.utc,
        )
    except (ValueError, TypeError):
        return None


def _compute_rollup_counts(
    open_tasks: list[dict[str, Any]] | None,
    completed_tasks: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Compute the three numeric facts for the 9 PM rollup shape.

    Returns dict with keys: tasks_added_today_count,
    tasks_completed_today_count, tasks_over_7d_count.

    Fail-open: if either input is None, that side's counts are 0. Better
    to narrate "0 added, 0 done" honestly than to silently omit the line
    or surface a partial number.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    today_midnight_utc = _today_midnight_pacific_utc(now)
    seven_days_ago = now - timedelta(days=7)

    added_today = 0
    over_7d = 0
    if open_tasks:
        for t in open_tasks:
            created = _parse_graph_iso(t.get("createdDateTime"))
            if created is None:
                continue
            if created >= today_midnight_utc:
                added_today += 1
            if created < seven_days_ago:
                over_7d += 1

    completed_today = 0
    if completed_tasks:
        for t in completed_tasks:
            # MS Graph wraps completedDateTime as {"dateTime": ..., "timeZone": ...}
            completed_field = t.get("completedDateTime")
            if isinstance(completed_field, dict):
                completed_field = completed_field.get("dateTime")
            completed_ts = _parse_graph_iso(completed_field)
            if completed_ts is None:
                continue
            # Also count completed-today entries against added-today if
            # they were ALSO created today. Mass-create-and-close is real
            # (Megha resolves a batch via Q&A) and the count should reflect
            # both events.
            created = _parse_graph_iso(t.get("createdDateTime"))
            if created is not None and created >= today_midnight_utc:
                added_today += 1
            if completed_ts >= today_midnight_utc:
                completed_today += 1

    return {
        "tasks_added_today_count": added_today,
        "tasks_completed_today_count": completed_today,
        "tasks_over_7d_count": over_7d,
    }


def _pick_top_importance_tasks(
    queued: list[dict[str, Any]],
    *,
    limit: int = 2,
) -> list[dict[str, Any]]:
    """Pick top 1-2 important tasks for the rollup's named-item slot.

    Importance signal today: `is_priority=True` on a queued entry.
    `is_priority` is set upstream in `inbox_to_task/handler.py` from
    `config.priority_senders` ("senders Megha cares about"). The signal
    sharpens as Megha adds entries to that config list.

    Returns a list of `{title, reason}` dicts, length 0 to `limit`. The
    composer reads these directly; no further filtering happens in the
    composer.

    Why this is its own selector and not embedded in `compose_periodic_
    summary`: the persona spec's three-axis rule says selection lives in
    selection.py and composition lives in composers/. Picking the top 1-2
    is a selection decision (which inputs reach the LLM), not a wording
    decision (how the LLM phrases them).
    """
    if not queued:
        return []
    out: list[dict[str, Any]] = []
    for q in queued:
        if not q.get("is_priority"):
            continue
        title = (q.get("title") or "").strip()
        if not title:
            continue
        # Reason text is best-effort. The signal is "this sender matters
        # to Megha" but the queued entry doesn't carry the matched sender
        # name today. Phase 2 can persist the sender; for v1 the LLM gets
        # the title and renders its own anchor per Anchoring rule.
        reason = q.get("priority_reason") or "from priority sender"
        out.append({"title": title, "reason": reason})
        if len(out) >= limit:
            break
    return out


# Owner-abbreviation prefix → person. Mirrors OWNER_ABBREV in
# `kavi_runtime/runtime/imessage_dispatch.py` (the title-render side of the
# same convention). Kept as a literal here rather than imported because
# importing imessage_dispatch drags in the full handler import chain and
# trips a circular import when selection is loaded standalone. The two maps
# are locked in sync by
# tests/test_owner_split_due_soon_selection.py::test_owner_prefix_map_matches_dispatch_abbrevs.
OWNER_PREFIX_TO_PERSON: dict[str, str] = {"MJ": "megha", "MM": "max"}


def _owner_prefix_map() -> dict[str, str]:
    """Return {abbrev: person} (e.g. {"MJ": "megha", "MM": "max"})."""
    return OWNER_PREFIX_TO_PERSON


def _owner_of_title(title: str | None) -> str:
    """Resolve a task title to its owner ("megha" / "max") via the owner
    abbreviation prefix the inbox-to-task runtime renders into every title
    (`MJ <title>` / `MM <title>`; see capabilities/inbox-to-task.md).

    The low-confidence `[?] ` prefix is stripped before matching, so
    `[?] MM Confirm daycare check` still routes to Max.

    Unprefixed titles (legacy tasks created before the abbreviation
    convention, or hand-typed entries) route to Megha — the household
    when-in-doubt-route-to-Megha rule.
    """
    t = (title or "").lstrip()
    if t.startswith("[?]"):
        t = t[3:].lstrip()
    for abbrev, person in _owner_prefix_map().items():
        if t == abbrev or t.startswith(abbrev + " "):
            return person
    return "megha"


def _filter_tasks_by_owner(
    tasks: list[dict[str, Any]] | None,
    owner: str,
    *,
    title_key: str = "title",
) -> list[dict[str, Any]] | None:
    """Keep only the tasks whose title-prefix owner is `owner`.

    Per-person split (2026-06-10): each digest/rollup recipient gets a
    compose over their own tasks only. Works on summary_queue entries and
    on raw MS Graph task objects alike (both carry `title`).

    None passes through as None so the fail-open contract of the fetch
    helpers (`_fetch_open_todo_tasks` → None on Graph error → counts 0)
    survives the owner split unchanged.
    """
    if tasks is None:
        return None
    return [
        t for t in tasks
        if _owner_of_title(t.get(title_key) or "") == owner
    ]


def _parse_graph_due_date(due_field: Any):
    """Parse an MS Graph `dueDateTime` field into a `datetime.date`.

    Graph wraps task due dates as `{"dateTime": "...", "timeZone": "..."}`;
    plain ISO strings are tolerated for forward compat. Due dates are
    date-shaped (midnight in some timezone, sometimes with 7-digit
    fractional seconds Python's fromisoformat rejects), so we read the
    leading `YYYY-MM-DD` rather than round-tripping through a datetime.

    Returns None on null / unparseable input; caller skips the task
    (most tasks have no due date — that is the normal case).
    """
    from datetime import date
    if isinstance(due_field, dict):
        due_field = due_field.get("dateTime")
    if not due_field or not isinstance(due_field, str):
        return None
    try:
        return date.fromisoformat(due_field[:10])
    except ValueError:
        return None


def _select_due_soon_tasks(
    open_tasks: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    runway_days: int | None = None,
) -> list[dict[str, Any]]:
    """Deadline runway: pick open tasks whose due date is within
    `DEADLINE_RUNWAY_DAYS` days or already past.

    Returns a list of `{title, due_date, days_until}` dicts sorted most
    urgent first (`days_until` ascending; negative = overdue by that many
    days). "Today" is Pacific — what Megha and Max live in — not UTC.

    Tasks with no due date (the majority) are skipped silently. Because
    the list is recomputed from open+due every morning, a due-soon task
    re-surfaces daily until it is closed in MS To Do — no repeat state.
    """
    if not open_tasks:
        return []
    if runway_days is None:
        runway_days = DEADLINE_RUNWAY_DAYS
    if now is None:
        now = datetime.now(timezone.utc)
    today_pacific = now.astimezone(_PACIFIC).date()
    out: list[dict[str, Any]] = []
    for t in open_tasks:
        due_date = _parse_graph_due_date(t.get("dueDateTime"))
        if due_date is None:
            continue
        days_until = (due_date - today_pacific).days
        if days_until > runway_days:
            continue
        title = (t.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "due_date": due_date.isoformat(),
            # Resolved here so the composer never derives a day name from
            # the bare date (2026-06-10 Verifier observation: the LLM wrote
            # "due Sunday" for a Monday date, 2/2 reproductions).
            "due_weekday": due_date.strftime("%A"),
            "days_until": days_until,
        })
    out.sort(key=lambda d: d["days_until"])
    return out


# ---- stale-task nudge (2026-06-11) ------------------------------------------

# Stale-task nudge: on a no-theme morning, the recipient's longest-open
# tasks become the close-out-old-threads frame — but only once they have
# been open STRICTLY LONGER than this many Pacific calendar days (a
# two-week-old task is normal backlog; day 15 is when it starts reading
# as a dropped thread). SELECTION-axis constant — it decides which inputs
# reach the composer, so its canonical home is this module (same contract
# as DEADLINE_RUNWAY_DAYS). The skill at
# `kavi-runtime/skills/periodic_summary_composer.md` references this
# constant by name and never inlines the number. PM feature request
# 2026-06-11 ("picking one or two tasks from an old list based on how
# long they have been open").
STALE_TASK_MIN_AGE_DAYS = 14


def _select_stale_task_nudge(
    open_tasks: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    max_items: int = 2,
) -> list[dict[str, Any]]:
    """Stale-task nudge: pick the recipient's longest-open tasks, oldest
    first, as the morning digest's close-out-old-threads frame.

    Returns a list of `{task_id, title, age_days}` dicts (at most
    `max_items`), sorted oldest first. `task_id` is carried so the caller can
    register a bindable pending question for the close-or-drop offer (a bare
    "keep"/"drop" reply needs the id to resolve through the qa executor).
    `age_days` counts Pacific calendar days between
    the task's `createdDateTime` and today — what Megha and Max live in,
    not UTC. Only tasks open strictly longer than
    `STALE_TASK_MIN_AGE_DAYS` qualify.

    Null / unparseable `createdDateTime` is skipped silently ("unknown"
    is not "old" — same contract as `_parse_graph_iso`). Like the
    deadline runway, the list derives from open tasks every morning with
    no repeat-surfacing state; `age_days` increments daily, so the
    debounce hash never swallows the repeat.

    The caller computes this ONLY when `_select_morning_theme` returned
    None (one top-of-mind frame per morning); the two are mutually
    exclusive by construction, not by composer judgment.
    """
    if not open_tasks:
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    today_pacific = now.astimezone(_PACIFIC).date()
    out: list[dict[str, Any]] = []
    for t in open_tasks:
        created = _parse_graph_iso(t.get("createdDateTime"))
        if created is None:
            continue
        title = (t.get("title") or "").strip()
        if not title:
            continue
        age_days = (today_pacific - created.astimezone(_PACIFIC).date()).days
        if age_days <= STALE_TASK_MIN_AGE_DAYS:
            continue
        out.append({"task_id": t.get("id"), "title": title, "age_days": age_days})
    out.sort(key=lambda d: d["age_days"], reverse=True)
    return out[:max_items]


# ---- morning top-of-mind theme (2026-06-10) --------------------------------

# A theme requires at least this many supporting open tasks. Selection-axis
# constant (canonical home: this module, same contract as
# DEADLINE_RUNWAY_DAYS). The clusterer skill at
# kavi-runtime/skills/morning_theme_clusterer.md references it by name;
# the code below ALSO enforces it on the LLM's output, so a weak cluster
# can't sneak through on prompt drift.
THEME_MIN_CLUSTER = 3

# Input caps for the clustering call (cost bound; worst-case token math in
# capabilities/kavi_persona/composers/theme_clusterer.py).
THEME_MAX_INPUT_TITLES = 40
THEME_TITLE_TRUNCATE_CHARS = 80

# How many of the chosen cluster's member task titles ride into the
# composer alongside {label, task_count} (added 2026-06-22). Selection-axis
# constant: it decides how many inputs reach the LLM, so its canonical home
# is this module (same contract as THEME_MIN_CLUSTER). The composer skill
# references "up to the cap" by behavior, never the literal. Capped low so
# the morning frame names a handful of concrete tasks without turning into
# an enumeration.
THEME_SURFACE_MAX_TITLES = 4

_THEME_STATE_FILENAME = "morning_theme.json"


def _morning_theme_state_path(config: dict) -> "Path":
    """Per-concept state file for the daily theme cache, sibling of the
    other per-concept files."""
    from pathlib import Path
    return Path(config["paths"]["imessage_state"]).parent / _THEME_STATE_FILENAME


def _select_morning_theme(
    config: dict,
    open_tasks: list[dict[str, Any]] | None,
    *,
    recipient: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Top-of-mind theme for the morning digest:
    `{label, task_count, task_titles}` or None (the common case).

    `task_titles` (added 2026-06-22) is the list of actual open-task titles
    that make up the chosen cluster — the same validated `supporting`
    titles the count is derived from — capped at THEME_SURFACE_MAX_TITLES,
    oldest first. This makes the morning digest actionable: the composer
    names the concrete tasks under the theme instead of only a vague label
    plus count. `label` and `task_count` are unchanged (backward
    compatible); a stale cache entry written before this key existed simply
    lacks `task_titles` and the composer falls back to the count-only frame.

    Behavior:
    - Fewer than THEME_MIN_CLUSTER open tasks → None, no LLM call (a
      cluster of >= THEME_MIN_CLUSTER cannot exist).
    - The clustering judgment runs ONCE per recipient per Pacific day;
      both outcomes (a theme, or the model's "no theme") cache in
      morning_theme.json. An API/parse failure returns None WITHOUT
      caching, so a transient failure doesn't suppress tomorrow's run.
    - The LLM's supporting_task_titles are validated against the actual
      input titles; a theme whose validated support drops below
      THEME_MIN_CLUSTER is treated as no-theme (conservative — a forced
      weak theme is worse than none).

    Cost caps: input is the newest THEME_MAX_INPUT_TITLES titles (the
    fetch order is lastModified desc), each truncated to
    THEME_TITLE_TRUNCATE_CHARS chars.
    """
    if not open_tasks or len(open_tasks) < THEME_MIN_CLUSTER:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    today = now.astimezone(_PACIFIC).date().isoformat()

    from kavi_runtime.state_io import atomic_write_json, read_json_recover
    state_path = _morning_theme_state_path(config)
    state = read_json_recover(state_path, default=None)
    if not isinstance(state, dict):
        state = {}
    cached = state.get(recipient)
    if isinstance(cached, dict) and cached.get("date") == today:
        theme = cached.get("theme")
        return theme if isinstance(theme, dict) else None

    titles = [
        (t.get("title") or "").strip()[:THEME_TITLE_TRUNCATE_CHARS]
        for t in open_tasks[:THEME_MAX_INPUT_TITLES]
        if (t.get("title") or "").strip()
    ]
    if len(titles) < THEME_MIN_CLUSTER:
        return None

    try:
        _, claude, _ = _clients_module._get_clients(config)
        result = claude.cluster_morning_theme(titles, today)
    except Exception as e:
        logger.warning(
            "morning_theme: clustering call failed (no theme today, not "
            "cached): %s", e,
        )
        return None
    if not isinstance(result, dict) or result.get("_error"):
        logger.warning(
            "morning_theme: clusterer error %s (no theme, not cached)",
            (result or {}).get("_error") if isinstance(result, dict) else "bad_result",
        )
        return None

    raw_theme = result.get("theme")
    theme_out: dict[str, Any] | None = None
    if isinstance(raw_theme, dict):
        label = (raw_theme.get("label") or "").strip()
        title_set = set(titles)
        # Validate each LLM-named supporting title back to an actual input
        # title (normalized to the truncated form the LLM saw). Keep the
        # normalized form so the surfaced titles match what was clustered.
        supporting = [
            s.strip()[:THEME_TITLE_TRUNCATE_CHARS]
            for s in (raw_theme.get("supporting_task_titles") or [])
            if isinstance(s, str) and s.strip()[:THEME_TITLE_TRUNCATE_CHARS] in title_set
        ]
        if label and len(supporting) >= THEME_MIN_CLUSTER:
            # Surface the cluster's member titles so the composer can name
            # the concrete tasks, not just a count (2026-06-22). Order
            # oldest-first: `titles` is newest-first (fetch is lastModified
            # desc), so a member's index in `titles` ranks recency; sort by
            # that index descending = oldest first. Dedupe defensively (the
            # LLM can repeat a title). Cap at THEME_SURFACE_MAX_TITLES.
            recency_rank = {t: i for i, t in enumerate(titles)}
            seen: set[str] = set()
            ordered_unique = [
                s for s in supporting
                if not (s in seen or seen.add(s))
            ]
            ordered_unique.sort(
                key=lambda s: recency_rank.get(s, -1), reverse=True,
            )
            task_titles = ordered_unique[:THEME_SURFACE_MAX_TITLES]
            theme_out = {
                "label": label[:60],
                "task_count": len(supporting),
                "task_titles": task_titles,
            }
        else:
            logger.info(
                "morning_theme: dropped weak/unsupported theme %r "
                "(validated support=%d, min=%d)",
                label[:40], len(supporting), THEME_MIN_CLUSTER,
            )

    try:
        state[recipient] = {"date": today, "theme": theme_out}
        atomic_write_json(state_path, state)
    except Exception as e:
        logger.warning("morning_theme: cache write failed (continuing): %s", e)
    return theme_out


def _pick_summary_anchor(
    queued: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the task that the periodic_summary most likely anchors on.

    Heuristic:
      1. First priority-flagged queued task.
      2. Else, first queued task.
      3. Else, first pending Q&A (its task_id, if known).
    """
    for q in queued or []:
        if q.get("is_priority") and q.get("task_id"):
            return {"task_id": q["task_id"], "title": q.get("title", "")}
    for q in queued or []:
        if q.get("task_id"):
            return {"task_id": q["task_id"], "title": q.get("title", "")}
    for p in pending or []:
        tid = p.get("task_id")
        if tid:
            title = p.get("task_title_rendered") or p.get("task_title") or ""
            return {"task_id": tid, "title": title}
    return None


__all__ = [
    "_fetch_open_todo_task_ids",
    "_fetch_open_todo_tasks",
    "_fetch_completed_todo_tasks",
    "_filter_summary_queue_by_open_status",
    "_filter_pending_questions_by_open_status",
    "_compute_rollup_counts",
    "_pick_top_importance_tasks",
    "_today_midnight_pacific_utc",
    "_parse_graph_iso",
    "_count_pending_facts",
    "_read_pending_facts_for_summary",
    "_pick_summary_anchor",
    "DEADLINE_RUNWAY_DAYS",
    "DUE_REMINDER_RUNWAY_DAYS",
    "OWNER_PREFIX_TO_PERSON",
    "_owner_of_title",
    "_filter_tasks_by_owner",
    "_parse_graph_due_date",
    "_select_due_soon_tasks",
    "STALE_TASK_MIN_AGE_DAYS",
    "_select_stale_task_nudge",
    "THEME_MIN_CLUSTER",
    "THEME_MAX_INPUT_TITLES",
    "THEME_TITLE_TRUNCATE_CHARS",
    "THEME_SURFACE_MAX_TITLES",
    "_morning_theme_state_path",
    "_select_morning_theme",
]
