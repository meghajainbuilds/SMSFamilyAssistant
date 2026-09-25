"""kavi-persona deep verify procedure.

Phase 4 (2026-06-02) physical move out of `kavi_runtime/synthetic_compose.py`.
Function bodies for `replay_periodic_summary`, `verify_periodic_summary_selection`,
`_title_keywords`, `_output_names_title`, `_detect_duplicate_phrases` live here.

Two endpoints today:
- `POST /synthetic/compose/kavi-persona` → `replay_periodic_summary`.
- `POST /synthetic/verify/kavi-persona` → `verify_periodic_summary_selection`.

Gates currently running in verify_periodic_summary_selection:
- `done_task_surfaced` — output names a title whose task_id is in
  `closed_task_ids`. Catches the 2026-05-30 9 PM rollup bug.
- `past_event_surfaced` — output names a title listed in
  `past_event_titles`. Catches same-day-but-past-time anchors.
- `duplicate_phrase` — same 4-word phrase appears twice in output.
- `count_without_axis` — count claim with no axis word or named example.
- `owner_leak` (2026-06-10, per-person split) — output composed for
  recipient R names a task whose title-prefix owner is the other person.
- `due_soon_dropped` (2026-06-10, deadline runway) — synthetic payload
  carries a due-soon item but the composed morning digest never
  references it.
- `close_claim` (2026-06-10, evening suggest-to-close) — payload carries
  close suggestions but the composed output uses completion-claim
  phrasing ("closed it", "marked done", "completed it") instead of
  suggestion language.
- `theme_unsupported` (2026-06-10, morning themes) — output's theme does
  not correspond to the payload's theme: payload theme present but its
  label keywords never appear, or no payload theme but the output uses a
  theme-introducing shape.
- `stale_task_dropped` (2026-06-11, stale-task nudge) — payload carries
  `stale_tasks` but the composed morning digest never references one of
  the items (at least one title keyword per item). No-op for rollups and
  for None output (mirror of `due_soon_dropped`).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.runtime.system_prompt import apply_skill_override as _apply_skill_override

logger = logging.getLogger(__name__)


# Gate categories tracked for the architectural test
# (kavi-runtime/tests/test_deep_verify_parity.py).
SHAPE_GATES: list[str] = [
    "length_cap",
    "prose_required",
    "no_banned_voice_substrings",
]
SELECTION_GATES: list[str] = [
    "done_task_surfaced",
    "past_event_surfaced",
    "duplicate_phrase",
    "count_without_axis",
    "owner_leak",
    "due_soon_dropped",
    "close_claim",
    "theme_unsupported",
    "stale_task_dropped",
]


# Axis words that qualify a count with a meaningful axis (time bound,
# status, age, action needed). Carefully excludes adjectives that ride
# along with the count itself ("new," "fresh," "queued") because those
# label the items without giving Megha a reason to act — e.g.,
# "7 NEW tasks in MS To Do" reads bare even though "new" is present.
_AXIS_WORDS: set[str] = {
    "today", "yesterday", "tomorrow", "tonight",
    "added", "done", "closed", "completed", "marked", "finished",
    "over", "older", "newer", "since",
    "week", "weeks", "day", "days", "ago",
    "decision", "decisions", "needs", "waiting", "due",
    "urgent", "priority", "important",
    "school", "work", "family", "personal",
}

# Pointer phrases that defer Megha to another surface. When a count
# appears in the same window as a pointer phrase and NO axis word is
# nearby, that is the canonical "count plus pointer" anti-pattern the
# persona spec calls out: "9 more tasks in MS To Do," "tap MS To Do
# for the list," etc.
_POINTER_PHRASES_RE = re.compile(
    r"\b(in|on|to|via|tap|check|open|see|go\s+to)\s+(ms\s*to\s*do|the\s+list|the\s+app|mstd)\b",
    re.IGNORECASE,
)

# A count claim: a number followed (optionally) by a qualifier and then a
# noun ("tasks," "emails," "items," "things"). Used to find every count
# claim in the output for axis-presence checking.
_COUNT_RE = re.compile(
    r"\b(\d+)\s+(?:new\s+|more\s+)?(tasks?|emails?|items?|things?)\b",
    re.IGNORECASE,
)


def replay_periodic_summary(
    config: dict[str, Any],
    state_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Replay the kavi-persona periodic_summary composer with a synthetic
    state snapshot. Returns the LLM output plus the input payload as evidence
    for the Verifier sub-agent.

    Phase 1 2026-06-02 shape: state_snapshot may carry the new fields
    tasks_added_today_count, tasks_completed_today_count, tasks_over_7d_count,
    top_importance_tasks. When absent, defaults preserve the pre-Phase-1
    contract (counts = 0, top_importance = []).

    2026-06-10: state_snapshot may also carry `recipient` ("megha" / "max",
    default "megha" — per-person split) and `due_soon` (deadline-runway
    items, each {title, due_date, days_until}).

    2026-06-10 (later same day): state_snapshot may also carry
    `close_suggestions` (evening suggest-to-close items, each
    {title, reason}) and `theme` ({label, task_count} — when the snapshot
    instead carries the clusterer's raw {label, supporting_task_titles}
    shape, task_count derives from the supporting list). Both additive.

    2026-06-11: state_snapshot may also carry `stale_tasks` (stale-task
    nudge items, each {title, age_days}, oldest first). Additive.
    """
    queued_tasks = state_snapshot.get("queued_tasks", [])
    pending_questions = state_snapshot.get("pending_questions", [])
    pending_facts = state_snapshot.get("pending_facts", [])
    is_rollup = bool(state_snapshot.get("is_rollup", False))
    time_of_day = state_snapshot.get("time_of_day", "morning")
    tasks_added_today = int(state_snapshot.get("tasks_added_today_count", 0))
    tasks_completed_today = int(state_snapshot.get("tasks_completed_today_count", 0))
    tasks_over_7d = int(state_snapshot.get("tasks_over_7d_count", 0))
    top_importance = state_snapshot.get("top_importance_tasks", []) or []
    recipient = (state_snapshot.get("recipient") or "megha").strip().lower()
    due_soon = state_snapshot.get("due_soon", []) or []
    close_suggestions = state_snapshot.get("close_suggestions", []) or []
    theme = _theme_for_composer(state_snapshot.get("theme"))
    stale_tasks = state_snapshot.get("stale_tasks", []) or []

    client = ClaudeClient(config)
    _apply_skill_override(client, state_snapshot)
    output = client.compose_periodic_summary(
        queued_tasks=queued_tasks,
        pending_questions=pending_questions,
        is_rollup=is_rollup,
        time_of_day=time_of_day,
        pending_facts=pending_facts,
        tasks_added_today_count=tasks_added_today,
        tasks_completed_today_count=tasks_completed_today,
        tasks_over_7d_count=tasks_over_7d,
        top_importance_tasks=top_importance,
        due_soon=due_soon,
        recipient_name=recipient.capitalize(),
        close_suggestions=close_suggestions,
        theme=theme,
        stale_tasks=stale_tasks,
    )

    model_routing = config.get("model_routing", {}) or {}
    model = model_routing.get(
        "compose_periodic_summary", config["claude"]["model"]
    )

    logger.info(
        "kavi_persona.verify.replay_periodic_summary: composed %d chars (model=%s)",
        len(output or ""),
        model,
    )

    return {
        "output": output,
        "model": model,
        "input_payload": {
            "queued_tasks": queued_tasks,
            "pending_questions": pending_questions,
            "pending_facts": pending_facts,
            "is_rollup": is_rollup,
            "time_of_day": time_of_day,
            "tasks_added_today_count": tasks_added_today,
            "tasks_completed_today_count": tasks_completed_today,
            "tasks_over_7d_count": tasks_over_7d,
            "top_importance_tasks": top_importance,
            "recipient": recipient,
            "due_soon": due_soon,
            "close_suggestions": close_suggestions,
            "theme": theme,
            "stale_tasks": stale_tasks,
        },
    }


def _theme_for_composer(raw: Any) -> dict[str, Any] | None:
    """Normalize a snapshot's `theme` field to the composer's
    {label, task_count} shape. Accepts the clusterer's raw
    {label, supporting_task_titles} shape (task_count derives from the
    supporting list) for Investigator convenience. None/invalid → None."""
    if not isinstance(raw, dict):
        return None
    label = (raw.get("label") or "").strip()
    if not label:
        return None
    task_count = raw.get("task_count")
    if not isinstance(task_count, int):
        supporting = raw.get("supporting_task_titles")
        task_count = len(supporting) if isinstance(supporting, list) else 0
    return {"label": label, "task_count": task_count}


# ---- Selection-behavior gates for periodic_summary ----

_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")


def _title_keywords(title: str, *, min_len: int = 4) -> list[str]:
    """Pull "anchoring" words out of a task title."""
    out: list[str] = []
    for tok in _WORD_TOKEN_RE.findall(title or ""):
        if len(tok) < min_len:
            continue
        if tok in {"Decide", "Confirm"}:
            continue
        out.append(tok)
    return out


def _output_names_title(output: str, title: str) -> bool:
    """Return True iff `output` plausibly names the task identified by
    `title`. Uses a keyword-overlap heuristic: any 2 distinct keywords from
    the title appearing in the output counts as a match."""
    if not output or not title:
        return False
    out_lower = output.lower()
    keywords = _title_keywords(title)
    hits = sum(1 for kw in keywords if kw.lower() in out_lower)
    return hits >= 2


def _detect_count_without_axis(
    output: str,
    top_importance_tasks: list[dict[str, Any]] | None,
) -> list[str]:
    """Find every count claim in `output` that lacks an axis word or a
    named example nearby.

    Returns a list of failure detail strings (one per offending count).
    Empty list = pass.

    The Aggregation rule in capabilities/kavi-persona.md requires every
    quoted count to come with either an axis (a qualifier word like
    "today," "done," "over a week") OR a named example (a specific task
    title the count refers to). Bare counts like "7 new tasks in MS To
    Do" are the canonical bad shape that shipped tonight's 9 PM rollup
    bug; this gate catches that shape post-compose, before the runtime
    Verifier would otherwise have to wait for Megha to notice.
    """
    if not output:
        return []

    # Build a set of title keywords from top_importance_tasks. A count
    # claim near one of these keywords passes the "named example" branch.
    named_keywords: set[str] = set()
    for t in (top_importance_tasks or []):
        for kw in _title_keywords((t.get("title") or "")):
            named_keywords.add(kw.lower())

    # Pre-compile a single axis regex with word boundaries so punctuation
    # ("today,") and end-of-string ("done.") both match.
    axis_re = re.compile(
        r"\b(" + "|".join(re.escape(w) for w in _AXIS_WORDS) + r")\b",
        re.IGNORECASE,
    )

    failures: list[str] = []
    for match in _COUNT_RE.finditer(output):
        count_phrase = match.group(0)
        # Window: ±60 chars around the match. Wide enough to catch axis
        # words in the same sentence; tight enough that a separate
        # paragraph or sentence doesn't accidentally cover.
        start = max(0, match.start() - 60)
        end = min(len(output), match.end() + 60)
        window = output[start:end].lower()

        # Count is OK if window has any axis word OR any named title kw.
        has_axis = bool(axis_re.search(window))
        has_named = any(kw in window for kw in named_keywords)

        # Negative signal: presence of a pointer phrase. Even with an
        # axis word, if the count is paired with a pointer and the
        # output reads as "N tasks, go check elsewhere," that is still
        # the bad shape. We treat pointer + no_axis as the harder fail
        # and pointer + axis as soft pass (Kavi can name an axis AND
        # also link to MS To Do for the rest, which is fine).
        has_pointer = bool(_POINTER_PHRASES_RE.search(window))

        if not has_axis and not has_named:
            if has_pointer:
                failures.append(
                    f"output contains count {count_phrase!r} paired with a "
                    f"pointer to MS To Do but no axis or named example; "
                    f"violates Aggregation rule in "
                    f"capabilities/kavi-persona.md (count plus pointer is "
                    f"deferral, not action)"
                )
            else:
                failures.append(
                    f"output contains bare count {count_phrase!r} with no "
                    f"axis qualifier (e.g., 'today,' 'over a week,' 'done') "
                    f"or named example nearby; violates Aggregation rule"
                )

    return failures


# Completion-claim phrasings forbidden when the payload carries close
# suggestions (close_claim gate, 2026-06-10). The skill's contract is
# suggestion language only ("Looks like you already handled X, want to
# close it?") — Kavi never claims the task was completed or that Kavi
# closed anything (the G-A1 action-claim rule's selection-side cousin).
# Present-tense suggestion forms deliberately do NOT match: "want to
# close it?", "want me to mark it done?" are the desired shape.
_CLOSE_COMPLETION_CLAIM_RES: list[re.Pattern[str]] = [
    # First-person completion claims: "I closed", "I've marked", "I have
    # completed", "I already closed".
    re.compile(r"\bI(?:'ve|\s+have|\s+already)?\s+(?:already\s+)?(?:closed|completed|marked)\b", re.IGNORECASE),
    # Past-tense claims about the task regardless of subject — the task is
    # in close_suggestions precisely because it is STILL OPEN, so "closed
    # it" / "completed that" is false no matter who the claimed actor is.
    re.compile(r"\b(?:closed|completed)\s+(?:it|that|them|both|the\s+task)\b", re.IGNORECASE),
    # "marked (it/that/them) (as) done/complete/completed" and bare
    # "marked done" / "marked as completed".
    re.compile(r"\bmarked\s+(?:(?:it|that|them|both)\s+)?(?:as\s+)?(?:done|complete|completed)\b", re.IGNORECASE),
]


def _detect_close_completion_claims(output: str) -> list[str]:
    """Find completion-claim phrasings in `output`. Returns one failure
    detail string per offending match; empty list = pass.

    Heuristic limits (documented on purpose): this is a phrase-pattern
    check, not a semantic one. Novel paraphrases ("that one's wrapped up",
    "all squared away") escape it — the upstream defenses are the skill's
    suggestion-language rule and the G-A1 action-claim gate at outbound
    time. When a new claim phrasing ships in production, add its pattern
    here (same maintenance contract as
    structural_checks.FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES).
    """
    if not output:
        return []
    failures: list[str] = []
    for pat in _CLOSE_COMPLETION_CLAIM_RES:
        for m in pat.finditer(output):
            failures.append(
                f"output contains completion-claim phrasing {m.group(0)!r} "
                f"while the payload carries close suggestions; suggestion "
                f"language only — the task is still open and Kavi closed "
                f"nothing (close_claim gate)"
            )
    return failures


# Theme-introducing shapes for the theme_unsupported gate's no-theme
# direction. When the payload carries NO theme, the composed output must
# not open a top-of-mind frame around an invented label.
_THEME_INTRO_RE = re.compile(
    r"\b(?:big\s+open\s+thread|top\s+of\s+mind|main\s+(?:open\s+)?thread|"
    r"biggest\s+(?:open\s+)?thread|the\s+big\s+theme|open\s+theme)\b",
    re.IGNORECASE,
)


def _detect_theme_unsupported(
    output: str,
    theme: dict[str, Any] | None,
) -> list[str]:
    """theme_unsupported gate (2026-06-10), two directions:

    1. Payload theme present → the output must correspond to it: at least
       one content keyword from the theme label must appear. A morning
       digest that names a theme must name THE theme it was given (and the
       skill says a present theme opens the message, so dropping it
       entirely is also a selection failure).
    2. No payload theme → the output must not invent one. Heuristic: the
       output must not contain a theme-introducing shape ("X is the big
       open thread", "top of mind", "the big theme").

    Heuristic limits (documented on purpose): direction 2 is a
    phrase-pattern check — an invented theme phrased without one of the
    introducing shapes escapes it, and we cannot enumerate every way to
    frame a theme. The check exists to catch the obvious invented-frame
    class cheaply; the per-day clustering cache plus the conservative
    clusterer skill are the upstream defenses. Direction 1 uses the same
    keyword heuristic as the other title-match gates (single-word labels
    shorter than the keyword minimum produce no keywords and the check
    no-ops rather than false-firing).
    """
    if not output:
        return []
    out_lower = output.lower()
    if theme and isinstance(theme, dict):
        label = (theme.get("label") or "").strip()
        keywords = _title_keywords(label)
        if not keywords:
            return []
        if not any(kw.lower() in out_lower for kw in keywords):
            return [
                f"payload carries theme label={label!r} but the composed "
                f"morning digest references none of its keywords; a "
                f"present theme must open the digest (theme_unsupported "
                f"gate, required-content direction)"
            ]
        return []
    m = _THEME_INTRO_RE.search(output)
    if m:
        return [
            f"payload carries NO theme but output uses theme-introducing "
            f"shape {m.group(0)!r}; the composer must not invent a "
            f"top-of-mind frame (theme_unsupported gate, invented-theme "
            f"direction)"
        ]
    return []


def _detect_duplicate_phrases(
    output: str, *, shingle_len: int = 4, min_word_len: int = 4,
) -> list[str]:
    """Find phrases that repeat verbatim in the composed output."""
    if not output:
        return []
    words = [w for w in _WORD_TOKEN_RE.findall(output) if len(w) >= min_word_len]
    if len(words) < shingle_len * 2:
        return []
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for i in range(len(words) - shingle_len + 1):
        shingle = " ".join(words[i:i + shingle_len]).lower()
        seen[shingle] = seen.get(shingle, 0) + 1
        if seen[shingle] == 2:
            duplicates.append(shingle)
    return duplicates


def verify_periodic_summary_selection(
    config: dict[str, Any],
    state_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Deep verify mode for periodic_summary. Runs the composer over the
    input snapshot and asserts selection-behavior gates on the composed
    output.
    """
    queued = state_snapshot.get("queued_tasks", []) or []
    pending = state_snapshot.get("pending_questions", []) or []
    facts = state_snapshot.get("pending_facts", []) or []
    is_rollup = bool(state_snapshot.get("is_rollup", False))
    time_of_day = state_snapshot.get("time_of_day", "morning")
    closed_task_ids = set(state_snapshot.get("closed_task_ids", []) or [])
    past_event_titles = list(state_snapshot.get("past_event_titles", []) or [])
    tasks_added_today = int(state_snapshot.get("tasks_added_today_count", 0))
    tasks_completed_today = int(state_snapshot.get("tasks_completed_today_count", 0))
    tasks_over_7d = int(state_snapshot.get("tasks_over_7d_count", 0))
    top_importance = state_snapshot.get("top_importance_tasks", []) or []
    recipient = (state_snapshot.get("recipient") or "megha").strip().lower()
    due_soon = state_snapshot.get("due_soon", []) or []
    close_suggestions = state_snapshot.get("close_suggestions", []) or []
    theme = _theme_for_composer(state_snapshot.get("theme"))
    stale_tasks = state_snapshot.get("stale_tasks", []) or []

    client = ClaudeClient(config)
    output = client.compose_periodic_summary(
        queued_tasks=queued,
        pending_questions=pending,
        is_rollup=is_rollup,
        time_of_day=time_of_day,
        pending_facts=facts,
        tasks_added_today_count=tasks_added_today,
        tasks_completed_today_count=tasks_completed_today,
        tasks_over_7d_count=tasks_over_7d,
        top_importance_tasks=top_importance,
        due_soon=due_soon,
        recipient_name=recipient.capitalize(),
        close_suggestions=close_suggestions,
        theme=theme,
        stale_tasks=stale_tasks,
    )
    model_routing = config.get("model_routing", {}) or {}
    model = model_routing.get(
        "compose_periodic_summary", config["claude"]["model"]
    )

    failures: list[dict[str, str]] = []

    if output and closed_task_ids:
        all_entries: list[tuple[str, str]] = []
        for q in queued:
            tid = q.get("task_id") or ""
            title = q.get("title") or ""
            all_entries.append((tid, title))
        for p in pending:
            tid = p.get("task_id") or ""
            title = p.get("task_title_rendered") or p.get("task_title") or ""
            all_entries.append((tid, title))
        for tid, title in all_entries:
            if tid in closed_task_ids and _output_names_title(output, title):
                failures.append({
                    "gate": "done_task_surfaced",
                    "detail": (
                        f"output names task_id={tid!r} title={title!r} which "
                        f"caller flagged as done in MS To Do; runtime filter "
                        f"should have dropped this upstream"
                    ),
                })

    if output and past_event_titles:
        for title in past_event_titles:
            if _output_names_title(output, title):
                failures.append({
                    "gate": "past_event_surfaced",
                    "detail": (
                        f"output names title={title!r} which caller flagged "
                        f"as past event; skill stale-date rule should have "
                        f"skipped this"
                    ),
                })

    if output:
        duplicates = _detect_duplicate_phrases(output)
        for dup in duplicates:
            failures.append({
                "gate": "duplicate_phrase",
                "detail": f"phrase {dup!r} appears twice in output",
            })

    if output:
        count_failures = _detect_count_without_axis(output, top_importance)
        for detail in count_failures:
            failures.append({
                "gate": "count_without_axis",
                "detail": detail,
            })

    # owner_leak (2026-06-10, per-person split): a digest composed for
    # recipient R must never name a task whose title-prefix owner is the
    # other person. Scans every titled input surface the composer saw.
    if output:
        from capabilities.kavi_persona.selection import _owner_of_title
        titled_inputs: list[str] = []
        for q in queued:
            titled_inputs.append(q.get("title") or "")
        for p in pending:
            titled_inputs.append(
                p.get("task_title_rendered") or p.get("task_title") or ""
            )
        for t in top_importance:
            titled_inputs.append(t.get("title") or "")
        for d in due_soon:
            titled_inputs.append(d.get("title") or "")
        for s in close_suggestions:
            titled_inputs.append(s.get("title") or "")
        for s in stale_tasks:
            titled_inputs.append(s.get("title") or "")
        for title in titled_inputs:
            if not title:
                continue
            title_owner = _owner_of_title(title)
            if title_owner != recipient and _output_names_title(output, title):
                failures.append({
                    "gate": "owner_leak",
                    "detail": (
                        f"output composed for recipient={recipient!r} names "
                        f"task title={title!r} whose title-prefix owner is "
                        f"{title_owner!r}; per-person selection filter "
                        f"should have dropped it upstream"
                    ),
                })

    # due_soon_dropped (2026-06-10, deadline runway): if the synthetic
    # payload carries a due-soon item, the composed morning digest must
    # reference it (at least one content keyword from the task title).
    # Morning-shape only — the 9 PM rollup keeps its counts + named-top
    # form, so the gate is a no-op when is_rollup is true.
    if output and due_soon and not is_rollup:
        out_lower = output.lower()
        for d in due_soon:
            title = d.get("title") or ""
            keywords = _title_keywords(title)
            if not keywords:
                continue
            if not any(kw.lower() in out_lower for kw in keywords):
                failures.append({
                    "gate": "due_soon_dropped",
                    "detail": (
                        f"payload carries due-soon item title={title!r} "
                        f"(days_until={d.get('days_until')!r}) but the "
                        f"composed morning digest references none of its "
                        f"content keywords; deadline-runway items must "
                        f"surface every morning until closed"
                    ),
                })

    # close_claim (2026-06-10, evening suggest-to-close): armed only when
    # the payload carries close suggestions — that is the contract under
    # test (suggestion language, never completion claims). Without
    # suggestions in the input, "done"-shaped wording is governed by the
    # G-A1 action-claim gate at outbound time, not this replay gate.
    if output and close_suggestions:
        for detail in _detect_close_completion_claims(output):
            failures.append({"gate": "close_claim", "detail": detail})

    # theme_unsupported (2026-06-10, morning themes): morning shape only —
    # the theme is a morning-digest frame, so the gate is a no-op for the
    # 9 PM rollup.
    if output and not is_rollup:
        for detail in _detect_theme_unsupported(output, theme):
            failures.append({"gate": "theme_unsupported", "detail": detail})

    # stale_task_dropped (2026-06-11, stale-task nudge): if the synthetic
    # payload carries stale-task nudge items, the composed morning digest
    # must reference each one (at least one content keyword from the task
    # title). Required-content gate, mirror of due_soon_dropped: the nudge
    # IS the morning's top-of-mind frame when present, so dropping an item
    # is a selection failure. No-op when is_rollup is true (the 9 PM
    # rollup keeps its counts + named-top shape and never carries a nudge).
    if output and stale_tasks and not is_rollup:
        out_lower = output.lower()
        for s in stale_tasks:
            title = s.get("title") or ""
            keywords = _title_keywords(title)
            if not keywords:
                continue
            if not any(kw.lower() in out_lower for kw in keywords):
                failures.append({
                    "gate": "stale_task_dropped",
                    "detail": (
                        f"payload carries stale-task nudge item "
                        f"title={title!r} (age_days={s.get('age_days')!r}) "
                        f"but the composed morning digest references none "
                        f"of its content keywords; the close-out-old-"
                        f"threads nudge is the morning frame when present "
                        f"and must surface every item"
                    ),
                })

    verdict = "PASS" if not failures else "FAIL"

    logger.info(
        "kavi_persona.verify.verify_periodic_summary_selection: verdict=%s "
        "failures=%d (model=%s, output=%r)",
        verdict, len(failures), model, (output or "")[:120],
    )

    return {
        "output": output,
        "verdict": verdict,
        "failures": failures,
        "model": model,
        "input_payload": {
            "queued_tasks": queued,
            "pending_questions": pending,
            "pending_facts": facts,
            "is_rollup": is_rollup,
            "time_of_day": time_of_day,
            "closed_task_ids": sorted(closed_task_ids),
            "past_event_titles": past_event_titles,
            "recipient": recipient,
            "due_soon": due_soon,
            "close_suggestions": close_suggestions,
            "theme": theme,
            "stale_tasks": stale_tasks,
        },
    }


__all__ = [
    "replay_periodic_summary",
    "verify_periodic_summary_selection",
    "_title_keywords",
    "_output_names_title",
    "_detect_duplicate_phrases",
    "_detect_count_without_axis",
    "_detect_close_completion_claims",
    "_detect_theme_unsupported",
    "SHAPE_GATES",
    "SELECTION_GATES",
]
