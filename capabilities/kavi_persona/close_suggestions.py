"""kavi-persona evening suggest-to-close — selection layer (added 2026-06-10).

PM goal: Megha manually closes lots of tasks that are actually done. The
evening rollup should SUGGEST closures it can infer from her own sent
email replies — never claim them, never close anything itself.

Selection flow per evening rollup (Megha only in v0 — her account's
sentitems is the only mailbox read; Max's can come later, see the spec
changelog in capabilities/kavi-persona.md):

1. Take the recipient's OPEN tasks (already owner-filtered upstream),
   newest createdDateTime first.
2. For each candidate (bounded scan, see cost caps): read the task's
   linked resource (externalId = source email message id written by
   inbox-to-task), resolve the email's conversationId, fetch Megha's SENT
   replies in that conversation, and keep replies newer than the task's
   createdDateTime.
3. For the newest such reply: the (task_id, reply_id) pair is judged by
   the close-suggestion LLM judge AT MOST ONCE EVER — verdicts cache in
   the per-concept state file `close_suggestions.json`.
4. A suggestion surfaces only when the judge says suggest_close=true AND
   confidence == "high" (conservative bias enforced in code).
5. A task already suggested in a prior rollup is NOT re-suggested unless
   a NEWER sent reply appears (the `suggested` map keys task_id →
   reply_id of the reply it was suggested on).

HARD COST CAPS (canonical home: this module — selection-axis constants,
same contract as DEADLINE_RUNWAY_DAYS in selection.py):

- CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN = 8 — at most 8 LLM judgments per
  evening run, newest tasks first. Worst-case nightly token cost is
  computed in composers/close_suggestion_judge.py (≈7,200 input + 1,200
  output tokens ≈ $0.04/night ≈ $1.20/month at Sonnet pricing).
- CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED = 16 — at most 16 candidate tasks
  examined per run, bounding the Graph reads (≤3 per candidate: linked
  resources + message fetch + sentitems query → ≤48 Graph reads/night).
  2× the judgment cap so cached pairs don't starve fresh candidates.

Every judgment (not just surfaced suggestions) appends an eval row to
eval-persona-close-suggestions.jsonl (path from config
`paths.eval_persona_close_suggestions_jsonl`) so Megha can label
precision later. Schema: evals/definitions.md.

State file shape (close_suggestions.json, atomic writes via state_io —
unique .tmp names, per-concept pattern):

    {
      "judged": {"<task_id>::<reply_id>": {"suggest_close": bool,
                                            "confidence": str,
                                            "reason": str|null,
                                            "ts": iso}},
      "suggested": {"<task_id>": {"reply_id": str, "ts": iso}}
    }

Failure posture: conservative no-suggest. Any per-task Graph error skips
that task; any top-level error returns []; an LLM API/parse failure is
NOT cached (transient — tomorrow retries) and never yields a suggestion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.runtime import clients as _clients_module
from kavi_runtime.state_io import atomic_write_json, read_json_recover

logger = logging.getLogger(__name__)

# ---- hard cost caps (see module docstring) ----------------------------------
CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN = 8
CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED = 16

_STATE_FILENAME = "close_suggestions.json"

_EMPTY_STATE: dict[str, Any] = {"judged": {}, "suggested": {}}


def _close_suggestions_state_path(config: dict) -> Path:
    """Per-concept state file, sibling of the other per-concept files
    (same directory as imessage_state, like periodic_summary_last_hash)."""
    return Path(config["paths"]["imessage_state"]).parent / _STATE_FILENAME


def _load_state(config: dict) -> dict[str, Any]:
    data = read_json_recover(
        _close_suggestions_state_path(config), default=None,
    )
    if not isinstance(data, dict):
        return {"judged": {}, "suggested": {}}
    return {
        "judged": data.get("judged") if isinstance(data.get("judged"), dict) else {},
        "suggested": data.get("suggested") if isinstance(data.get("suggested"), dict) else {},
    }


def _save_state(config: dict, state: dict[str, Any]) -> None:
    atomic_write_json(_close_suggestions_state_path(config), state)


def _pair_key(task_id: str, reply_id: str) -> str:
    return f"{task_id}::{reply_id}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _append_judgment_eval_row(config: dict, row: dict[str, Any]) -> None:
    """Append one judgment row to eval-persona-close-suggestions.jsonl.
    No-throw: eval logging never blocks the rollup."""
    try:
        path = (config.get("paths") or {}).get(
            "eval_persona_close_suggestions_jsonl"
        )
        if not path:
            logger.debug(
                "close_suggestions: eval path not configured; skipping row",
            )
            return
        from kavi_runtime.state import append_jsonl
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(p, row)
    except Exception as e:
        logger.warning("close_suggestions: eval row append failed: %s", e)


def _source_email_id_for_task(
    graph: Any, list_id: str, task_id: str, account: str | None = None,
) -> str | None:
    """Read the task's linked resources and return the source email message
    id (externalId), preferring the HomeOS-written resource. None when the
    task has no linked resource (hand-created tasks — common)."""
    resources = graph.list_todo_task_linked_resources(list_id, task_id)
    if not isinstance(resources, list):
        return None
    fallback: str | None = None
    for lr in resources:
        if not isinstance(lr, dict):
            continue
        ext = lr.get("externalId")
        if not ext or not isinstance(ext, str):
            continue
        if (lr.get("applicationName") or "").startswith("HomeOS"):
            return ext
        if fallback is None:
            fallback = ext
    return fallback


def _newest_reply_after(
    replies: list[dict[str, Any]], created: datetime,
) -> dict[str, Any] | None:
    """Newest sent reply strictly newer than the task's createdDateTime,
    or None."""
    best: dict[str, Any] | None = None
    best_ts: datetime | None = None
    for r in replies or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        sent = _parse_iso(r.get("sentDateTime"))
        if sent is None or sent <= created:
            continue
        if best_ts is None or sent > best_ts:
            best, best_ts = r, sent
    return best


def _msg_from_address(m: dict[str, Any]) -> str:
    return (
        ((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    ).strip().lower()


def _newest_inbound_after(
    messages: list[dict[str, Any]],
    created: datetime,
    own_addresses: set[str],
) -> dict[str, Any] | None:
    """Newest INBOUND thread message (from someone other than the recipient)
    strictly newer than the task's createdDateTime — the "response to an
    email" done-signal (e.g. a vendor replying 'payment received'). Returns
    a candidate normalized to the sent-reply shape ({id, sentDateTime,
    bodyPreview}) so the judge call site stays uniform, or None.

    `own_addresses` are the recipient's own addresses (lowercased); a
    message from one of those is a sent reply, already covered by
    `_newest_reply_after`, so it is excluded here to avoid double-judging.
    """
    best: dict[str, Any] | None = None
    best_ts: datetime | None = None
    for m in messages or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        if _msg_from_address(m) in own_addresses:
            continue  # our own sent message — handled by the sent-reply path
        ts = _parse_iso(m.get("receivedDateTime"))
        if ts is None or ts <= created:
            continue
        if best_ts is None or ts > best_ts:
            best, best_ts = m, ts
    if best is None:
        return None
    return {
        "id": best["id"],
        "sentDateTime": best.get("receivedDateTime"),
        "bodyPreview": best.get("bodyPreview") or "",
        "_direction": "inbound",
    }


def _pick_newest_candidate(
    sent: dict[str, Any] | None, inbound: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Pick the newer of a sent-reply and an inbound-reply candidate (both
    normalized to carry `sentDateTime`). Either may be None."""
    if sent is None:
        return inbound
    if inbound is None:
        return sent
    st = _parse_iso(sent.get("sentDateTime"))
    it = _parse_iso(inbound.get("sentDateTime"))
    if it is None:
        return sent
    if st is None:
        return inbound
    return inbound if it > st else sent


def select_close_suggestions(
    config: dict,
    open_tasks: list[dict[str, Any]] | None,
    *,
    account: str | None = None,
    own_addresses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return close suggestions for the evening rollup: each
    `{task_id, title, reason, reply_id}`, newest task first.

    `open_tasks` is the recipient's owner-filtered open task list (raw MS
    Graph objects). `account` selects whose mailbox to read (per-person
    rollup, 2026-06-22): Megha's tasks read Megha's mailbox, Max's read
    Max's. `own_addresses` (lowercased) are that recipient's own email
    addresses; a thread message from one of them is a sent reply, anything
    else is an inbound "response to an email" done-signal. See module
    docstring for caps + caching.
    """
    if not open_tasks:
        return []
    own_addresses = {a.strip().lower() for a in (own_addresses or set()) if a}
    try:
        list_id = (config.get("graph") or {}).get("mstodo_shared_list_id")
        if not list_id:
            return []
        graph, claude, _ = _clients_module._get_clients(config)
        state = _load_state(config)
        judged: dict[str, Any] = state["judged"]
        suggested: dict[str, Any] = state["suggested"]

        # Newest created first — the cap spends judgments on recent tasks,
        # where a sent reply most plausibly reflects fresh completion.
        def _created_key(t: dict[str, Any]) -> str:
            return t.get("createdDateTime") or ""

        candidates = sorted(open_tasks, key=_created_key, reverse=True)

        suggestions: list[dict[str, Any]] = []
        judgments_used = 0
        state_dirty = False

        for task in candidates[:CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED]:
            task_id = task.get("id")
            title = (task.get("title") or "").strip()
            created = _parse_iso(task.get("createdDateTime"))
            if not task_id or not title or created is None:
                continue
            try:
                email_id = _source_email_id_for_task(
                    graph, list_id, task_id, account=account,
                )
                if not email_id:
                    continue
                msg = graph.fetch_message(email_id, account=account)
                conv_id = (msg or {}).get("conversationId")
                if not conv_id:
                    continue
                replies = graph.fetch_sentitems_replies(conv_id, account=account)
                # Inbound "response to an email" done-signal (2026-06-22).
                # Best-effort + isolated: a stub/runtime without fetch_thread,
                # or a thread-read failure, must NEVER break the proven
                # sent-reply path — fall back to inbound = none.
                thread_msgs: list[dict[str, Any]] = []
                try:
                    thread_msgs = graph.fetch_thread(conv_id, account=account)
                except Exception as e:
                    logger.debug(
                        "close_suggestions: fetch_thread unavailable/failed "
                        "for task %s (inbound signal skipped): %s",
                        str(task_id)[:12], e,
                    )
            except Exception as e:
                logger.warning(
                    "close_suggestions: Graph read failed for task %s "
                    "(skipping task): %s", str(task_id)[:12], e,
                )
                continue

            # The done-signal is the NEWEST of: the recipient's own sent
            # reply, OR an inbound reply from the other party. Both are
            # "a response to the email" Megha cares about; the newest wins.
            sent_reply = _newest_reply_after(replies, created)
            inbound_reply = _newest_inbound_after(
                thread_msgs, created, own_addresses,
            )
            reply = _pick_newest_candidate(sent_reply, inbound_reply)
            if reply is None:
                continue
            reply_id = reply["id"]

            # Already suggested on this exact reply → never re-suggest
            # until a NEWER reply appears (a newer reply changes reply_id).
            if (suggested.get(task_id) or {}).get("reply_id") == reply_id:
                continue

            key = _pair_key(task_id, reply_id)
            verdict = judged.get(key)
            if verdict is None:
                if judgments_used >= CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN:
                    continue
                result = claude.judge_close_suggestion(
                    task_title=title,
                    task_created_at=task.get("createdDateTime") or "",
                    reply_preview=reply.get("bodyPreview") or "",
                    reply_sent_at=reply.get("sentDateTime") or "",
                    reply_direction=reply.get("_direction", "sent"),
                )
                judgments_used += 1
                if not isinstance(result, dict) or result.get("_error"):
                    # Transient LLM failure: no suggestion, no cache —
                    # tomorrow's run re-judges. Conservative no-suggest.
                    logger.warning(
                        "close_suggestions: judge failed for task %s (%s); "
                        "no suggestion", str(task_id)[:12],
                        (result or {}).get("_error") if isinstance(result, dict) else "bad_result",
                    )
                    continue
                verdict = {
                    "suggest_close": bool(result.get("suggest_close")),
                    "confidence": result.get("confidence", "low"),
                    "reason": result.get("reason"),
                    "ts": _utc_now_iso(),
                }
                judged[key] = verdict
                state_dirty = True
                _append_judgment_eval_row(config, {
                    "ts": verdict["ts"],
                    "capability": "kavi-persona",
                    "kind": "close_suggestion_judgment",
                    "task_id": task_id,
                    "task_title": title,
                    "reply_id": reply_id,
                    "reply_sent_at": reply.get("sentDateTime"),
                    "suggest_close": verdict["suggest_close"],
                    "confidence": verdict["confidence"],
                    "reason": verdict["reason"],
                    "usage": result.get("_usage"),
                })

            # Conservative surfacing: suggest only on a definitive,
            # high-confidence yes.
            if verdict.get("suggest_close") and verdict.get("confidence") == "high":
                suggestions.append({
                    "task_id": task_id,
                    "title": title,
                    "reason": (verdict.get("reason") or "").strip()
                              or "your sent reply reads as already handled",
                    "reply_id": reply_id,
                })

        if state_dirty:
            _save_state(
                config, {"judged": judged, "suggested": suggested},
            )
        return suggestions
    except Exception as e:
        logger.warning(
            "close_suggestions: selection failed (continuing without "
            "suggestions): %s", e,
        )
        return []


def record_close_suggestions_surfaced(
    config: dict, suggestions: list[dict[str, Any]],
) -> None:
    """Mark suggestions as surfaced so they are not re-suggested until a
    NEWER sent reply appears.

    Engineering call: every suggestion handed to the composer is marked,
    not just the ones the LLM named — items past the
    CLOSE_SUGGESTIONS_SURFACE_MAX cap were at least counted in the "N more
    might be closable" mention, and re-suggesting them nightly would read
    as nagging. A marked-but-unacted suggestion resurfaces as soon as
    Megha replies again on that thread.
    """
    if not suggestions:
        return
    try:
        state = _load_state(config)
        now = _utc_now_iso()
        for s in suggestions:
            task_id = s.get("task_id")
            reply_id = s.get("reply_id")
            if not task_id or not reply_id:
                continue
            state["suggested"][task_id] = {"reply_id": reply_id, "ts": now}
        _save_state(config, state)
    except Exception as e:
        logger.warning(
            "close_suggestions: surfaced-state write failed: %s", e,
        )


__all__ = [
    "CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN",
    "CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED",
    "select_close_suggestions",
    "record_close_suggestions_surfaced",
    "_close_suggestions_state_path",
]
