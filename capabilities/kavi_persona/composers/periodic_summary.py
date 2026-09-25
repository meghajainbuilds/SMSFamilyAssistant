"""kavi-persona periodic_summary composer + handler.

Phase 4 (2026-06-02) physical move. Holds:
- `compose_periodic_summary` (LLM call body; ClaudeClient method is a proxy)
- `_periodic_summary_state_path`, `_periodic_summary_input_hash`,
  `_load_periodic_summary_last_hash`, `_save_periodic_summary_last_hash`,
  `_should_suppress_periodic_summary` (debounce machinery)
- `periodic_summary`, `_periodic_summary_impl` (the scheduler-driven entry
  point + impl; produces the 7am / 11am / 3pm / 7pm / 9pm rollup iMessage).

PERSONA: kavi_persona (loaded via persona_loader inside `_build_system_prompt`).
STRUCTURAL CONSTRAINTS: length caps via `structural_checks.LENGTH_CAP_TARGET`
and `LENGTH_CAP_HARD`. JSON-shape gate inside compose_periodic_summary.
BEHAVIOR: kavi-runtime/skills/periodic_summary_composer.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.structural_checks import (
    CLOSE_SUGGESTIONS_SURFACE_MAX,
    LENGTH_CAP_HARD,
    LENGTH_CAP_TARGET,
)

# Module-level imports so tests can monkeypatch via this module's namespace.
from kavi_runtime.state import (
    list_pending_questions,
    add_pending_question,
    drop_expired_questions,
    STALE_NUDGE_QUESTION_TTL_SECONDS,
    drain_summary_queue,
    save_summary_anchor,
    append_run,
    utc_now_iso,
)
from kavi_runtime.runtime.clients import _get_clients
from kavi_runtime.runtime.send_imessage import _send_imessage_with_fallback
from kavi_runtime.runtime.paths import _runtime_events_path

logger = logging.getLogger(__name__)


_PERIODIC_SUMMARY_SAFE_FALLBACK = (
    "Pausing today's summary while I sort the inputs. "
    "Check MS To Do for the list."
)

PERIODIC_SUMMARY_DEBOUNCE_HOURS = 36

# Per-person mailbox identity for evening close-suggestions (2026-06-22).
# Addresses come from the private config's household block
# (kavi_runtime/household.py). `account` = the
# OAuth mailbox to read; `own` = that person's own addresses (a thread
# message from one of these is their sent reply, anything else is an inbound
# "response to an email" done-signal). Max is only read when his token is
# present (has_token guard) — degrades silently if his mailbox isn't wired.
from kavi_runtime import household as _household

_PERSON_MAILBOX: dict[str, dict[str, Any]] = {
    name: {
        "account": _household.primary_email(name),
        "own": set(_household.member_emails(name)),
    }
    for name in ("megha", "max")
}


def _est_input_tokens(system: list[dict[str, Any]], user_msg: str) -> int:
    try:
        system_chars = sum(len(b.get("text", "")) for b in system if isinstance(b, dict))
    except Exception:
        system_chars = 0
    return (system_chars + len(user_msg or "")) // 4


def compose_periodic_summary(
    client: "ClaudeClient",  # type: ignore[name-defined]
    queued_tasks: list[dict[str, Any]],
    pending_questions: list[dict[str, Any]],
    is_rollup: bool,
    time_of_day: str,
    pending_facts: list[dict[str, Any]] | None = None,
    *,
    tasks_added_today_count: int = 0,
    tasks_completed_today_count: int = 0,
    tasks_over_7d_count: int = 0,
    top_importance_tasks: list[dict[str, Any]] | None = None,
    due_soon: list[dict[str, Any]] | None = None,
    recipient_name: str = "Megha",
    close_suggestions: list[dict[str, Any]] | None = None,
    theme: dict[str, Any] | None = None,
    stale_tasks: list[dict[str, Any]] | None = None,
    due_reminders: list[dict[str, Any]] | None = None,
) -> str | None:
    """Compose a periodic summary message via Sonnet using v0.1 persona prompt.

    Returns the composed message string (<=120 chars target, <=180 chars hard
    cap), or None on error or over-length. Caller falls back to a safe
    sentence on None (cold-fallback policy 2026-05-29).

    Phase 1 2026-06-02: three rollup counts + top_importance_tasks added as
    kwargs. Morning digest path ignores the counts (skill says no counts at
    7 AM); 9 PM rollup path uses them as the three numeric facts. The skill
    at kavi-runtime/skills/periodic_summary_composer.md drives shape; the
    composer only marshals input.

    2026-06-10: `due_soon` (deadline-runway items selected upstream by
    `selection._select_due_soon_tasks`; each {title, due_date, days_until})
    and `recipient_name` ("Megha" / "Max", per-person split) added. The
    composer marshals both into the input payload; the skill owns how they
    surface.

    2026-06-10 (later same day): `close_suggestions` (evening
    suggest-to-close items selected upstream by
    `close_suggestions.select_close_suggestions`; each {title, reason})
    and `theme` ({label, task_count} | None, morning top-of-mind theme
    from `selection._select_morning_theme`) added. Both are ADDITIVE: the
    payload keys appear only when there is content, so a null theme /
    empty suggestions leave the input shape byte-identical to the
    pre-feature contract. The skill owns how they surface; the user_msg
    surfaces the CLOSE_SUGGESTIONS_SURFACE_MAX numeric cap (structural
    constant) when suggestions are present.

    2026-06-11: `stale_tasks` (stale-task nudge items selected upstream
    by `selection._select_stale_task_nudge`, each {title, age_days},
    oldest first) added. ADDITIVE like close_suggestions/theme: the
    payload key appears only when non-empty. The caller passes it only
    on no-theme mornings; the composer just marshals.
    """
    system = client._build_system_prompt("periodic_summary_composer")

    input_payload = {
        "is_rollup": is_rollup,
        "time_of_day": time_of_day,
        "recipient": recipient_name,
        "due_soon": [
            {
                "title": (d.get("title") or "")[:120],
                "due_date": d.get("due_date"),
                "due_weekday": d.get("due_weekday"),
                "days_until": d.get("days_until"),
            }
            for d in (due_soon or [])
        ],
        "tasks_added_today_count": tasks_added_today_count,
        "tasks_completed_today_count": tasks_completed_today_count,
        "tasks_over_7d_count": tasks_over_7d_count,
        "top_importance_tasks": [
            {
                "title": (t.get("title") or "")[:120],
                "reason": (t.get("reason") or "")[:60],
            }
            for t in (top_importance_tasks or [])
        ],
        "queued_tasks": [
            {
                "title": t.get("title", ""),
                "owner": t.get("owner"),
                "is_priority": t.get("is_priority", False),
            }
            for t in queued_tasks
        ],
        "pending_questions": [
            {
                "task_title": q.get("task_title_rendered") or q.get("task_title", ""),
                "asked_at": q.get("asked_at"),
            }
            for q in pending_questions
        ],
        "pending_facts": [
            {
                "topic": (pf.get("topic") or "")[:30],
                # Full fact text, no cap (2026-06-10): selection owns
                # what reaches the LLM and already strips audit
                # boilerplate; re-capping here re-introduced the starved
                # "coordinating with Max on something" digest bug. The
                # LLM compresses to the outbound length cap itself.
                "snippet": pf.get("snippet") or "",
            }
            for pf in (pending_facts or [])
        ],
    }
    # Additive keys (2026-06-10): present only with content so the input
    # shape stays unchanged for callers without these features (and the
    # debounce hash of legacy inputs is unaffected).
    if close_suggestions:
        input_payload["close_suggestions"] = [
            {
                "title": (s.get("title") or "")[:120],
                "reason": (s.get("reason") or "")[:200],
            }
            for s in close_suggestions
        ]
    if theme:
        input_payload["theme"] = {
            "label": (theme.get("label") or "")[:60],
            "task_count": theme.get("task_count"),
        }
    if stale_tasks:
        input_payload["stale_tasks"] = [
            {
                "title": (s.get("title") or "")[:120],
                "age_days": s.get("age_days"),
            }
            for s in stale_tasks
        ]
    # Due-date reminders (2026-06-22): 9 PM evening run only. Each item is a
    # task with a due date that is tomorrow (days_until==1, day-before nudge),
    # today (days_until==0, day-of follow-up), or overdue-but-open
    # (days_until<0). The skill phrases each per days_until; days_until and
    # due_weekday are resolved upstream so the LLM never derives a weekday.
    if due_reminders:
        input_payload["due_reminders"] = [
            {
                "title": (d.get("title") or "")[:120],
                "due_date": d.get("due_date"),
                "due_weekday": d.get("due_weekday"),
                "days_until": d.get("days_until"),
            }
            for d in due_reminders
        ]

    close_cap_line = ""
    if close_suggestions:
        # Numeric cap surfaced from the structural constant, never inlined
        # in the skill (three-axis rule).
        close_cap_line = (
            f"Name at most {CLOSE_SUGGESTIONS_SURFACE_MAX} of the "
            f"close_suggestions items; summarize any remainder as a "
            f"might-be-closable count without naming them.\n\n"
        )

    user_msg = (
        f"<input>\n{json.dumps(input_payload, indent=2)}\n</input>\n\n"
        f"Compose ONE periodic summary message for iMessage to "
        f"{recipient_name} per the skill procedure.\n\n"
        f"{close_cap_line}"
        "Output EXACTLY this JSON shape and NOTHING ELSE:\n"
        f'  {{"message": "<your message, <={LENGTH_CAP_TARGET} chars>"}}\n\n'
        "Do NOT write reasoning prose. Do NOT write 'Looking at the input', "
        "'**Stale-date check:**', '- ... — Skip', or any other chain-of-"
        "thought. Do NOT write preamble or explanation. Reason internally; "
        "emit the JSON object only. Your entire response is the JSON object."
    )

    model = client._model_for_call_type("compose_periodic_summary")
    started = client._log_call_start(
        "compose_periodic_summary", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        try:
            usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
            logger.info("compose_periodic_summary usage: %s", usage)
        except Exception:
            usage = None
        text = resp.content[0].text if resp.content else ""
        client._log_call_done("compose_periodic_summary", model, started, usage,
                              input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        if not parsed or "message" not in parsed:
            logger.warning("compose_periodic_summary: bad output %r", text[:200])
            return None
        msg = (parsed.get("message") or "").strip()
        if not msg:
            logger.warning("compose_periodic_summary: empty message")
            return None
        if len(msg) > LENGTH_CAP_HARD:
            logger.warning("compose_periodic_summary: over-length %d chars, falling back", len(msg))
            return None
        return msg
    except Exception as e:
        client._log_call_failed("compose_periodic_summary", started, e)
        logger.exception("compose_periodic_summary failed: %s", e)
        return None


# ---- debounce machinery ----

def _periodic_summary_state_path(config: dict) -> Path:
    """Path to the small JSON state file that records the most recent
    periodic_summary fire's input hash."""
    state_dir = Path(config["paths"]["imessage_state"]).parent
    return state_dir / "periodic_summary_last_hash.json"


def _periodic_summary_input_hash(
    queued: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    pending_facts: list[dict[str, Any]],
    due_soon: list[dict[str, Any]] | None = None,
    *,
    close_suggestions: list[dict[str, Any]] | None = None,
    theme: dict[str, Any] | None = None,
    stale_tasks: list[dict[str, Any]] | None = None,
    due_reminders: list[dict[str, Any]] | None = None,
) -> str:
    """Stable dedup hash over the composer's INPUT payload.

    2026-06-10: deadline-runway items hash as (title, days_until) pairs.
    `days_until` decrements every day, so a due-soon task changes the hash
    daily — the debounce never swallows the intentional repeat-every-
    morning-until-closed behavior, while a same-day double fire still
    dedups.

    2026-06-10 (later same day): close suggestions hash as (title,
    reply_id) pairs and the theme as its label — a new suggestion or a
    theme change busts yesterday's hash. Both keys join the payload ONLY
    when non-empty so every pre-feature hash value (including the ones
    already persisted in periodic_summary_last_hash.json on Kavi) stays
    stable.

    2026-06-11: stale-task nudge items hash as (title, age_days) pairs —
    same contract as due_soon: `age_days` increments every day, so the
    intentional repeat-every-morning-until-closed behavior is never
    swallowed by the debounce, while a same-day double fire still dedups.
    Joins the payload only when non-empty (legacy hashes stable).
    """
    queued_titles = sorted(
        (t.get("title") or "").strip()
        for t in (queued or [])
    )
    pending_titles = sorted(
        (q.get("task_title_rendered") or q.get("task_title") or "").strip()
        for q in (pending or [])
    )
    fact_topics = sorted(
        (pf.get("topic") or "").strip()
        for pf in (pending_facts or [])
    )
    due_soon_keys = sorted(
        [(d.get("title") or "").strip(), d.get("days_until")]
        for d in (due_soon or [])
    )
    hash_input: dict[str, Any] = {
        "queued": queued_titles,
        "pending": pending_titles,
        "facts": fact_topics,
        "due_soon": due_soon_keys,
    }
    if close_suggestions:
        hash_input["close_suggestions"] = sorted(
            [(s.get("title") or "").strip(), s.get("reply_id") or ""]
            for s in close_suggestions
        )
    if theme:
        hash_input["theme"] = (theme.get("label") or "").strip()
    if stale_tasks:
        hash_input["stale_tasks"] = sorted(
            [(s.get("title") or "").strip(), s.get("age_days")]
            for s in stale_tasks
        )
    if due_reminders:
        # (title, days_until) — days_until decrements daily so a due-date
        # reminder changes the hash every day (day-before -> day-of), never
        # swallowed by the 36h debounce, while a same-day double fire dedups.
        hash_input["due_reminders"] = sorted(
            [(d.get("title") or "").strip(), d.get("days_until")]
            for d in due_reminders
        )
    payload = json.dumps(
        hash_input,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _load_periodic_summary_last_hash(
    config: dict, *, recipient: str = "megha",
) -> dict[str, Any] | None:
    """Return the prior fire's {hash, ts, kind} dict for `recipient`, or
    None if missing / unreadable.

    Per-recipient keying (2026-06-10, per-person split): Megha's entry IS
    the legacy top-level {hash, ts, kind} shape — the pre-split state file
    migrates as hers with no rewrite. Other recipients live under the
    `recipients` sub-dict keyed by person name.
    """
    path = _periodic_summary_state_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        logger.warning(
            "periodic_summary: last-hash read failed (continuing send): %s", e,
        )
        return None
    if not isinstance(data, dict):
        return None
    if recipient == "megha":
        return data if data.get("hash") else None
    entry = (data.get("recipients") or {}).get(recipient)
    return entry if isinstance(entry, dict) and entry.get("hash") else None


def _save_periodic_summary_last_hash(
    config: dict, *, hash_value: str, kind: str, recipient: str = "megha",
) -> None:
    """Atomic write of the most recent fire's hash for `recipient`.

    Read-modify-write so one recipient's fire never clobbers the other's
    debounce state. Megha keeps the legacy top-level shape (see
    `_load_periodic_summary_last_hash`); other recipients nest under
    `recipients`. atomic_write_json serializes before touching disk and
    writes via a unique tmp name (per-concept state pattern).
    """
    from kavi_runtime.state_io import atomic_write_json
    path = _periodic_summary_state_path(config)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except Exception as e:
            logger.warning(
                "periodic_summary: last-hash read-before-write failed "
                "(starting fresh): %s", e,
            )
    entry = {
        "hash": hash_value,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
    }
    if recipient == "megha":
        data.update(entry)
    else:
        data.setdefault("recipients", {})[recipient] = entry
    atomic_write_json(path, data)


def _should_suppress_periodic_summary(
    config: dict, new_hash: str, *, now: datetime | None = None,
    recipient: str = "megha",
) -> tuple[bool, dict[str, Any] | None]:
    """Decide whether to suppress this periodic_summary fire based on a
    duplicate-content check, per recipient."""
    if now is None:
        now = datetime.now(timezone.utc)
    prior = _load_periodic_summary_last_hash(config, recipient=recipient)
    if not prior:
        return False, None
    if prior.get("hash") != new_hash:
        return False, prior
    prior_ts = prior.get("ts")
    if not prior_ts:
        return False, prior
    try:
        prior_dt = datetime.fromisoformat(prior_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return False, prior
    age = now - prior_dt
    if age.total_seconds() < PERIODIC_SUMMARY_DEBOUNCE_HOURS * 3600:
        return True, prior
    return False, prior


# ---- scheduler entry point ----

def periodic_summary(config: dict, is_rollup: bool = False) -> None:
    """Scheduler-driven digest iMessage (7am / 11am / 3pm / 7pm / 9pm cron).
    Wraps the body in a Trace context. Failure-safe."""
    try:
        from kavi_runtime.trace_log import Trace as _Trace
        _trace_ctx = _Trace(
            channel="scheduler", config=config, capability="kavi-persona",
        )
    except Exception as _e:
        logger.debug("trace_log: Trace construction failed (continuing): %s", _e)
        _trace_ctx = None
    if _trace_ctx is None:
        return _periodic_summary_impl(config, is_rollup)
    with _trace_ctx:
        return _periodic_summary_impl(config, is_rollup)


def _periodic_summary_impl(config: dict, is_rollup: bool = False) -> None:
    """Send a digest iMessage. Body of the scheduler entry point."""
    # Selection imports at function scope only because the selection module
    # imports back from this module's namespace would create a cycle. Keep
    # them as locals so tests can monkeypatch via
    # `capabilities.kavi_persona.selection` if needed.
    from capabilities.kavi_persona.selection import (
        _fetch_open_todo_task_ids,
        _fetch_open_todo_tasks,
        _fetch_completed_todo_tasks,
        _filter_summary_queue_by_open_status,
        _filter_pending_questions_by_open_status,
        _filter_tasks_by_owner,
        _select_due_soon_tasks,
        DUE_REMINDER_RUNWAY_DAYS,
        _select_morning_theme,
        _select_stale_task_nudge,
        _compute_rollup_counts,
        _pick_top_importance_tasks,
        _count_pending_facts,
        _read_pending_facts_for_summary,
        _pick_summary_anchor,
    )
    from capabilities.kavi_persona.close_suggestions import (
        select_close_suggestions,
        record_close_suggestions_surfaced,
    )

    state_path = Path(config["paths"]["imessage_state"])
    pending = list_pending_questions(state_path)
    queued = drain_summary_queue(state_path)
    open_ids = _fetch_open_todo_task_ids(config)
    queued = _filter_summary_queue_by_open_status(
        config, queued, open_ids=open_ids,
    )
    pending = _filter_pending_questions_by_open_status(
        config, pending, open_ids=open_ids,
    )

    try:
        from kavi_runtime.runtime import durable_facts
        pruned_count = durable_facts.prune_expired_pending_facts(config=config)
        if pruned_count:
            logger.info(
                "periodic_summary: pruned %d expired pending fact(s) pre-digest",
                pruned_count,
            )
    except Exception as e:
        logger.warning(
            "periodic_summary: prune_expired_pending_facts failed (continuing): %s",
            e,
        )

    # Pending facts are now read PER-PERSON inside the loop (scope-aware,
    # 2026-06-23): a fact's `scope` routes it to the person it's about, so a
    # Max-scoped coordination commitment no longer leaks into Megha's digest.

    kind_label = "rollup" if is_rollup else "summary"
    graph, claude, _ = _get_clients(config)
    time_of_day = "9pm" if is_rollup else "morning"

    # Phase 1 2026-06-02: rollup gets three numeric facts + top 1-2 named.
    # Morning digest gets only top 1-2 named (no counts per skill). Counts
    # are still computed for both shapes so the same code path serves
    # synthetic-replay tests; the skill ignores them at morning.
    open_tasks = _fetch_open_todo_tasks(config)
    completed_tasks = (
        _fetch_completed_todo_tasks(config) if is_rollup else None
    )

    # Per-person split (2026-06-10): one compose + one send per household
    # recipient, each over their own tasks only (title-prefix owner;
    # unprefixed → Megha). Pending Q&A + pending facts are Megha-facing by
    # design (coordination Q&A routes to her), so Max's input carries only
    # his tasks / counts / due-soon items.
    for person in ("megha", "max"):
        recipient_phone = config.get("imessage", {}).get(f"{person}_phone", "")
        if person == "max" and not recipient_phone:
            logger.info(
                "periodic_summary: max_phone not configured; skipping Max",
            )
            continue

        r_queued = _filter_tasks_by_owner(queued, person) or []
        r_pending = pending if person == "megha" else []
        # Pending-fact confirmation is Megha-admin-facing (she's the household
        # fact-confirmer); Max's digest carries none. But now scope-filtered
        # (2026-06-23): Megha sees megha/household/unset facts, NEVER a
        # scope:max coordination commitment — fixing the Premier-Mechanical
        # leak without introducing fact-prompts to Max.
        r_facts = (
            _read_pending_facts_for_summary(config, recipient="megha")
            if person == "megha" else []
        )
        r_facts_count = (
            _count_pending_facts(config, recipient="megha")
            if person == "megha" else 0
        )
        r_open = _filter_tasks_by_owner(open_tasks, person)
        r_completed = _filter_tasks_by_owner(completed_tasks, person)
        counts = _compute_rollup_counts(r_open, r_completed)
        # Deadline runway is a morning-digest shape; the 9 PM rollup keeps
        # its three-counts + named-top form.
        due_soon = _select_due_soon_tasks(r_open) if not is_rollup else []

        # Due-date reminders (2026-06-22, Megha): 9 PM evening run only.
        # Day-before nudge (due tomorrow) + day-of follow-up (due today, still
        # open) + overdue-but-open. Per-person loop routes each person's
        # reminders to that person automatically. Failure-safe via the
        # selector's own empty-on-error contract.
        due_reminders = (
            _select_due_soon_tasks(r_open, runway_days=DUE_REMINDER_RUNWAY_DAYS)
            if is_rollup else []
        )

        has_content = bool(
            r_queued or r_pending or r_facts_count
            or due_soon or due_reminders or any(counts.values())
        )
        # Engineering call (2026-06-10, flagged for PM review in the spec
        # changelog): Max gets a message ONLY when his selection is
        # non-empty. All-quiet sends nothing to Max. Megha always receives
        # hers, including the all-clear shape.
        if person == "max" and not has_content:
            logger.info(
                "periodic_summary: Max selection empty, skipping his send "
                "(is_rollup=%s)", is_rollup,
            )
            continue

        # Evening suggest-to-close (2026-06-10; both persons 2026-06-22).
        # Each person's rollup reads THEIR OWN mailbox so a Max-owned done
        # task asks Max (the per-person send routes it). Max is read only
        # when his token is present (has_token guard). Signal = the newest
        # of the recipient's own sent reply OR an inbound "response to an
        # email" in the thread. Failure-safe: any selector error means no
        # suggestions, never a blocked rollup.
        close_suggestions: list[dict[str, Any]] = []
        if is_rollup:
            mbox = _PERSON_MAILBOX.get(person)
            if mbox:
                acct = mbox["account"]
                try:
                    if graph.has_token(acct):
                        close_suggestions = select_close_suggestions(
                            config, r_open,
                            account=acct, own_addresses=mbox["own"],
                        )
                    else:
                        logger.info(
                            "periodic_summary: no token for %s; skipping "
                            "close-suggestions for %s", acct, person,
                        )
                except Exception as e:
                    logger.warning(
                        "periodic_summary: close-suggestion selection failed "
                        "(continuing without): %s", e,
                    )
                    close_suggestions = []

        # Morning top-of-mind theme (2026-06-10). Computed AFTER the
        # has_content gate on purpose: a theme is a framing device over
        # tasks Megha/Max already see, not new information, so it never
        # triggers a send by itself (and Max's skipped send never pays a
        # clustering call). Cached per recipient per day upstream.
        theme: dict[str, Any] | None = None
        if not is_rollup:
            try:
                theme = _select_morning_theme(
                    config, r_open, recipient=person,
                )
            except Exception as e:
                logger.warning(
                    "periodic_summary: theme selection failed "
                    "(continuing without): %s", e,
                )
                theme = None

        # Stale-task nudge (2026-06-11): morning only, and ONLY when no
        # cluster theme fired — one top-of-mind frame per morning, and the
        # theme wins when both could exist (mutual exclusion lives HERE,
        # by construction, not in composer judgment). due_soon items are
        # never displaced; they ride alongside the nudge. Failure-safe:
        # a selector error means no nudge, never a blocked digest.
        stale_tasks: list[dict[str, Any]] = []
        if not is_rollup and theme is None:
            try:
                stale_tasks = _select_stale_task_nudge(r_open)
            except Exception as e:
                logger.warning(
                    "periodic_summary: stale-task nudge selection failed "
                    "(continuing without): %s", e,
                )
                stale_tasks = []

        input_hash = _periodic_summary_input_hash(
            r_queued, r_pending, r_facts, due_soon=due_soon,
            close_suggestions=close_suggestions, theme=theme,
            stale_tasks=stale_tasks, due_reminders=due_reminders,
        )
        should_suppress, prior_state = _should_suppress_periodic_summary(
            config, input_hash, recipient=person,
        )
        if should_suppress:
            prior_ts = (prior_state or {}).get("ts")
            logger.info(
                "periodic_summary: SUPPRESSED — identical input hash %s as "
                "prior fire at %s (is_rollup=%s, recipient=%s)",
                input_hash, prior_ts, is_rollup, person,
            )
            try:
                from kavi_runtime.outbound_log import log_outbound
                log_outbound(
                    config,
                    kind="periodic_summary_suppressed",
                    text="",
                    send_result={"verified": False, "fallback_used": False},
                    context={
                        "is_rollup": is_rollup,
                        "suppression_reason": "identical_input_hash",
                        "input_hash": input_hash,
                        "prior_fire_ts": prior_ts,
                        "queued_count": len(r_queued),
                        "pending_count": len(r_pending),
                        "pending_facts_count": r_facts_count,
                        "recipient": person,
                    },
                )
            except Exception as e:
                logger.warning(
                    "periodic_summary: suppression log row write failed: %s",
                    e,
                )
            continue

        top_importance = _pick_top_importance_tasks(r_queued, limit=2)
        logger.info(
            "periodic_summary: recipient=%s counts=%s top_importance=%d "
            "due_soon=%d stale_tasks=%d (is_rollup=%s)",
            person, counts, len(top_importance), len(due_soon),
            len(stale_tasks), is_rollup,
        )

        msg = claude.compose_periodic_summary(
            r_queued, r_pending, is_rollup, time_of_day,
            pending_facts=r_facts,
            tasks_added_today_count=counts["tasks_added_today_count"],
            tasks_completed_today_count=counts["tasks_completed_today_count"],
            tasks_over_7d_count=counts["tasks_over_7d_count"],
            top_importance_tasks=top_importance,
            due_soon=due_soon,
            recipient_name=person.capitalize(),
            close_suggestions=close_suggestions,
            theme=theme,
            stale_tasks=stale_tasks,
            due_reminders=due_reminders,
        )

        # Suggestions were actually composed into a message only when the
        # LLM succeeded; the safe fallback carries none, so don't burn the
        # one-shot surfacing state on it.
        close_suggestions_composed = msg is not None and bool(close_suggestions)

        # Provenance for the outbound: LLM compose by default; the safe
        # fallback below carries its cold-fallback audit date instead.
        msg_provenance = {"llm_call": "compose_periodic_summary"}

        if msg is None:
            logger.warning(
                "periodic_summary: LLM composition failed; sending safe "
                "message (is_rollup=%s, recipient=%s, queued=%d, pending=%d)",
                is_rollup, person, len(r_queued), len(r_pending),
            )
            msg = _PERIODIC_SUMMARY_SAFE_FALLBACK
            msg_provenance = {"fallback_audit": "2026-05-29"}

        anchor = _pick_summary_anchor(r_queued, r_pending)
        if anchor:
            try:
                save_summary_anchor(
                    state_path,
                    recipient_phone,
                    anchor_task_id=anchor["task_id"],
                    anchor_task_title=anchor["title"],
                    summary_message=msg,
                )
                logger.info(
                    "periodic_summary: saved anchor task_id=%s title=%r "
                    "(recipient=%s)",
                    anchor["task_id"][:12], anchor["title"][:80], person,
                )
            except Exception as e:
                logger.warning(
                    "periodic_summary: save_summary_anchor failed: %s", e,
                )

        if person == "megha":
            # Megha's send keeps the legacy call shape (recipient defaults
            # to megha_phone inside the wrapper). Several long-standing
            # test fixtures monkeypatch the send wrapper with a
            # (config, text, *, kind) signature; the default-recipient path
            # stays byte-compatible with them.
            send_result = _send_imessage_with_fallback(
                config, msg, kind="periodic_summary",
                provenance=msg_provenance,
            )
        else:
            # Max's send names his handle explicitly AND suppresses the
            # Outlook fallback: the fallback path resolves its recipient to
            # Megha regardless of addressee (known bug, in backlog), so a
            # failed BlueBubbles send to Max must NOT land his rollup in
            # Megha's inbox. A skipped fallback is safer than misdelivery.
            send_result = _send_imessage_with_fallback(
                config, msg, kind="periodic_summary",
                recipient_handle=recipient_phone,
                suppress_outlook_fallback=True,
                provenance=msg_provenance,
            )
        sent = send_result["verified"] or send_result["fallback_used"]
        logger.info(
            "periodic_summary: verified=%s fallback=%s (is_rollup=%s, "
            "recipient=%s, queued=%d, pending=%d)",
            send_result["verified"], send_result["fallback_used"],
            is_rollup, person, len(r_queued), len(r_pending),
        )

        if close_suggestions_composed and sent:
            try:
                record_close_suggestions_surfaced(config, close_suggestions)
            except Exception as e:
                logger.warning(
                    "periodic_summary: close-suggestion surfaced-state "
                    "write failed: %s", e,
                )

        # Register the stale-task close-or-drop offer as bindable pending
        # questions, so a later bare "keep"/"drop" resolves THESE tasks through
        # the qa executor (a real tool result, G-A1-grounded) instead of
        # degrading to the conversational path and tripping the action-claim
        # gate (the 2026-06-26 "keep" → "I caught myself…" bug). Gated on a
        # verified/fallback send: an unsent or suppressed digest must never
        # leave a question Megha never saw. task_title_rendered is the task's
        # VERBATIM current title — the qa-keep executor PATCHes the title to
        # this value, so a verbatim title makes "keep" an idempotent no-op
        # (grounds the gate, renames nothing). expires_at caps an unanswered
        # nudge so it can't resurface stale next morning.
        if stale_tasks and sent and msg is not None:
            expires_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=STALE_NUDGE_QUESTION_TTL_SECONDS)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            for st in stale_tasks:
                st_id = st.get("task_id")
                if not st_id:
                    continue
                try:
                    add_pending_question(state_path, {
                        "id": f"q-{uuid.uuid4().hex[:8]}",
                        "kind": "q_and_a",
                        "task_id": st_id,
                        "task_title_rendered": st["title"],
                        "source_subject": "Stale-task close-or-drop nudge",
                        "asked_at": utc_now_iso(),
                        "expires_at": expires_at,
                    })
                except Exception as e:
                    logger.warning(
                        "periodic_summary: stale-nudge pending-question "
                        "registration failed (task_id=%s): %s",
                        str(st_id)[:12], e,
                    )

        try:
            _save_periodic_summary_last_hash(
                config, hash_value=input_hash, kind=kind_label,
                recipient=person,
            )
        except Exception as e:
            logger.warning(
                "periodic_summary: last-hash write failed (continuing): %s", e,
            )

        try:
            append_run(_runtime_events_path(config), {
                "run_id": utc_now_iso(),
                "event_type": "periodic_summary",
                "ts": utc_now_iso(),
                "is_rollup": is_rollup,
                "recipient": person,
                "queued_count": len(r_queued),
                "pending_count": len(r_pending),
                "sent": sent,
                "annotations": None,
            })
        except Exception as e:
            logger.warning("runs.jsonl append failed (periodic_summary): %s", e)


__all__ = [
    "compose_periodic_summary",
    "periodic_summary",
    "_periodic_summary_impl",
    "_PERIODIC_SUMMARY_SAFE_FALLBACK",
    "PERIODIC_SUMMARY_DEBOUNCE_HOURS",
    "_periodic_summary_state_path",
    "_periodic_summary_input_hash",
    "_load_periodic_summary_last_hash",
    "_save_periodic_summary_last_hash",
    "_should_suppress_periodic_summary",
]
