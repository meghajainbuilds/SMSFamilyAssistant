"""Capability function: parse_reply_intents — the LLM intent parser at the
head of the intent-first iMessage dispatch (PM-approved rebuild, 2026-06-10).

Every semantic inbound goes through this ONE call. Output is the structured
intent list (contract: evals/kavi-reply/matrix/ENDPOINT_CONTRACT.md §3);
deterministic executors in `capabilities/kavi_persona/intent_executors.py`
act on it; the final reply composer narrates the results.

Three-axis split:
- PERSONA   -> capabilities/kavi-persona.md (not loaded here — classifier call)
- STRUCTURE -> output JSON shape validated below
- BEHAVIOR  -> kavi-runtime/skills/reply_intent_parser.md

Cold-fallback policy (CLAUDE.md 2026-05-29): ONE retry with a tighter
prompt, then the caller ships one deterministic safe sentence. This module
never fabricates intents on failure — it returns None so the dispatch can
take the safe-sentence path.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kavi_runtime.claude_client import ClaudeClient

logger = logging.getLogger(__name__)

# Closed canonical vocabulary (ENDPOINT_CONTRACT.md §3). Gate matching and
# executor dispatch both key on this set.
CANONICAL_INTENT_TYPES: frozenset[str] = frozenset({
    "qa_keep", "qa_drop",
    "close_task", "create_task", "delete_task", "rename_task",
    "undo", "pause", "resume",
    "correction", "coordination_reply",
    "clarify", "conversational",
})

# Intent types whose targets resolve against open_tasks (vs pending_questions).
_TASK_TARGET_TYPES = frozenset({"close_task", "delete_task", "rename_task"})
_QUESTION_TARGET_TYPES = frozenset({"qa_keep", "qa_drop"})

# Owner-prefix convention (household.md): MJ = Megha, MM = Max,
# unprefixed = Megha.
_OWNER_PREFIX = {"megha": "MJ", "max": "MM"}


def _now_pt_str() -> str:
    """Current wall-clock in America/Los_Angeles as the parser's date anchor:
    `YYYY-MM-DD (Weekday) HH:MM PT`. Callers on the live message path should
    pass the inbound's own timestamp; this is the fallback."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime.now(ZoneInfo("America/Los_Angeles"))
    return dt.strftime("%Y-%m-%d (%A) %H:%M PT")


def _title_owner(title: str) -> str:
    t = (title or "").strip()
    if t.startswith("[?] "):
        t = t[4:]
    if t.startswith("MM "):
        return "max"
    return "megha"


def _est_input_tokens(system: list, user_msg: str) -> int:
    total = sum(len(b.get("text", "")) for b in system)
    total += len(user_msg)
    return int(total / 4)


def _validate_and_normalize(
    parsed: Any,
    *,
    open_tasks: list[dict[str, Any]],
    pending_questions: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Validate the parser's JSON into the canonical intent list.

    Returns None on an unusable shape (caller retries / falls back).
    Per-intent salvage: invented target ids are dropped; an intent of a
    task/question-target type left with ZERO valid targets degrades to
    `clarify` (never silently executes against nothing, never invents).
    """
    if not isinstance(parsed, dict) or not isinstance(parsed.get("intents"), list):
        return None
    task_titles = {
        t.get("id"): (t.get("title") or "")
        for t in open_tasks if isinstance(t, dict) and t.get("id")
    }
    question_titles = {
        q.get("id"): (q.get("task_title_rendered") or q.get("task_title") or "")
        for q in pending_questions if isinstance(q, dict) and q.get("id")
    }
    out: list[dict[str, Any]] = []
    for raw in parsed["intents"]:
        if not isinstance(raw, dict):
            continue
        itype = raw.get("type")
        if itype not in CANONICAL_INTENT_TYPES:
            logger.warning("parse_reply_intents: unknown intent type %r; skipping", itype)
            continue
        target_text = raw.get("target_text")
        if not isinstance(target_text, str):
            target_text = ""
        confidence = raw.get("confidence")
        if confidence not in {"high", "low"}:
            confidence = "low"
        valid_pool = (
            task_titles if itype in _TASK_TARGET_TYPES
            else question_titles if itype in _QUESTION_TARGET_TYPES
            else {**task_titles, **question_titles}
        )
        targets: list[dict[str, str]] = []
        for t in raw.get("targets") or []:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            if tid not in valid_pool:
                logger.warning(
                    "parse_reply_intents: invented/foreign target id %r on %s; dropping",
                    tid, itype,
                )
                continue
            targets.append({"id": tid, "title": valid_pool[tid]})
        if itype in (_TASK_TARGET_TYPES | _QUESTION_TARGET_TYPES) and not targets:
            # Target-shaped intent with nothing real to act on: degrade to
            # clarify rather than execute-nothing or guess.
            logger.info(
                "parse_reply_intents: %s with no valid targets -> clarify "
                "(target_text=%r)", itype, target_text[:80],
            )
            itype = "clarify"
        norm: dict[str, Any] = {
            "type": itype,
            "target_text": target_text,
            "targets": targets,
            "confidence": confidence,
        }
        if itype == "create_task":
            # Carry the parser's extracted task fields through. The executor
            # consumes task_title/owner/due and falls back to target_text /
            # sender only when these are absent (intent-extraction bug class:
            # verbatim title, owner-by-sender, no due date).
            raw_title = raw.get("task_title")
            if isinstance(raw_title, str) and raw_title.strip():
                norm["task_title"] = raw_title.strip()
            raw_owner = raw.get("owner")
            if raw_owner in ("megha", "max"):
                norm["owner"] = raw_owner
            raw_due = raw.get("due")
            if isinstance(raw_due, str) and raw_due.strip():
                norm["due"] = raw_due.strip()
        out.append(norm)
    return out


# 1024 cut off a correct "clarify" mid-reasoning on 2026-09-25 (17k-token
# input). Output is small JSON; the cap only bounds runaway prose.
PARSER_MAX_TOKENS = 2048

# Pending questions that are close offers ("mark done or keep?"). Today
# only the morning stale-task nudge registers one. Megha 2026-09-25:
# "close" and "drop" mean the same thing on these.
CLOSE_OFFER_SUBJECTS = ("Stale-task close-or-drop nudge",)

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "them", "those",
    "these", "task", "tasks", "close", "done", "mark", "drop", "both", "all",
    "please", "yes", "okay", "out", "one", "ones", "it's", "they", "those",
})


def _is_close_offer(q: dict[str, Any]) -> bool:
    return bool(q.get("task_id")) and str(q.get("source_subject") or "").startswith(CLOSE_OFFER_SUBJECTS)


def _names_task(text: str, title: str) -> bool:
    """True when `text` shares a distinctive word (4+ letters, not a
    stopword or owner prefix) with the task title."""
    import re as _re
    words = lambda s: {w for w in _re.findall(r"[a-z0-9']{4,}", (s or "").lower())} - _STOPWORDS
    return bool(words(text) & words(title))


def _bind_to_close_offer(
    intents: list[dict[str, Any]],
    *,
    pending_questions: list[dict[str, Any]],
    inbound_text: str,
) -> list[dict[str, Any]]:
    """Hard check (2026-09-25 incident). While a close offer is pending, a
    close/delete may only touch tasks the offer named, unless the message
    names the task itself ("close the Lesa survey"). Anything else becomes
    `clarify` with the offered tasks as candidates. And a `qa_drop` on a
    close offer IS a close of that task (Megha: close = drop)."""
    offers = [q for q in pending_questions if _is_close_offer(q)]
    if not offers:
        return intents
    offer_tasks = {q["task_id"]: (q.get("task_title_rendered") or q.get("task_title") or "") for q in offers}
    offer_qids = {q.get("id"): q["task_id"] for q in offers}
    out: list[dict[str, Any]] = []
    for intent in intents:
        if intent["type"] == "qa_drop":
            as_close = [t for t in intent["targets"] if t["id"] in offer_qids]
            rest = [t for t in intent["targets"] if t["id"] not in offer_qids]
            if as_close:
                out.append({**intent, "type": "close_task", "targets": [
                    {"id": offer_qids[t["id"]], "title": offer_tasks[offer_qids[t["id"]]]}
                    for t in as_close]})
            if rest:
                out.append({**intent, "targets": rest})
            continue
        if intent["type"] in ("close_task", "delete_task") and intent["targets"]:
            stray = [t for t in intent["targets"]
                     if t["id"] not in offer_tasks
                     and not _names_task(inbound_text, t["title"])
                     and not _names_task(intent.get("target_text", ""), t["title"])]
            if stray:
                logger.warning(
                    "parse_reply_intents: %s on tasks outside the pending close offer "
                    "and not named in the message -> clarify (stray=%s)",
                    intent["type"], [t["title"][:40] for t in stray],
                )
                intent = {**intent, "type": "clarify", "targets": [
                    {"id": tid, "title": title} for tid, title in offer_tasks.items()]}
        out.append(intent)
    return out


def _apply_deterministic_rules(
    intents: list[dict[str, Any]],
    *,
    sender: str,
    pending_questions: list[dict[str, Any]],
    inbound_text: str = "",
) -> list[dict[str, Any]]:
    """Two deterministic post-parse enforcements (defense in depth on top of
    the skill's instructions; both are contract-level rules):

    1. Cross-owner mutation -> clarify, ONE DIRECTION: a close/delete/rename
       by MAX whose every target is Megha's downgrades to clarify
       (single-owner accountability: Kavi confirms before mutating her task
       on his word). Megha mutating MM tasks executes normally — she
       administers the household system. The frozen matrix encodes the
       asymmetry; the symmetric version of this rule failed
       spec-seven-mark-done-one-message on the 2026-06-11 maiden run (the
       sweep includes an MM task in Megha's close request).
    2. Canonical direction: when a close_task targets a task that also has a
       pending question, any qa_keep/qa_drop on that same question is dropped
       — the close wins and resolves the question as answered-by-close.
    """
    intents = _bind_to_close_offer(
        intents, pending_questions=pending_questions, inbound_text=inbound_text,
    )
    out: list[dict[str, Any]] = []
    for intent in intents:
        if (
            sender == "max"
            and intent["type"] in _TASK_TARGET_TYPES
            and intent["targets"]
        ):
            owners = {_title_owner(t["title"]) for t in intent["targets"]}
            if owners and sender not in owners:
                logger.info(
                    "parse_reply_intents: cross-owner %s by sender=%s -> clarify "
                    "(targets=%s)", intent["type"], sender,
                    [t["title"][:40] for t in intent["targets"]],
                )
                intent = {**intent, "type": "clarify"}
        out.append(intent)

    # Canonical-direction dedupe: collect question ids covered by a close.
    closed_titles = {
        t["title"]
        for i in out if i["type"] == "close_task"
        for t in i["targets"]
    }
    closed_task_ids = {
        t["id"]
        for i in out if i["type"] == "close_task"
        for t in i["targets"]
    }
    questions_covered_by_close: set[str] = set()
    for q in pending_questions:
        qid = q.get("id")
        q_title = q.get("task_title_rendered") or q.get("task_title") or ""
        q_task_id = q.get("task_id")
        if (q_task_id and q_task_id in closed_task_ids) or (
            q_title and q_title in closed_titles
        ):
            if qid:
                questions_covered_by_close.add(qid)
    if questions_covered_by_close:
        deduped: list[dict[str, Any]] = []
        for intent in out:
            if intent["type"] in _QUESTION_TARGET_TYPES:
                remaining = [
                    t for t in intent["targets"]
                    if t["id"] not in questions_covered_by_close
                ]
                if not remaining:
                    logger.info(
                        "parse_reply_intents: %s dropped — its question is "
                        "covered by a close_task (canonical direction)",
                        intent["type"],
                    )
                    continue
                intent = {**intent, "targets": remaining}
            deduped.append(intent)
        out = deduped
    return out


def parse_reply_intents(
    client,
    *,
    inbound_text: str,
    sender: str,
    now: str | None = None,
    recent_outbound: list[dict[str, Any]] | None = None,
    recent_inbound: list[str] | None = None,
    pending_questions: list[dict[str, Any]] | None = None,
    pending_facts: list[dict[str, Any]] | None = None,
    open_tasks: list[dict[str, Any]] | None = None,
    open_coordination_sessions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]] | None:
    """Run the LLM intent parser over one inbound with full context.

    Returns the validated intent list, or None when both the call and its
    single tighter-prompt retry failed (caller takes the safe-sentence path
    per the cold-fallback policy — never a fabricated intent list).
    """
    pending_questions = pending_questions or []
    open_tasks = open_tasks or []
    if not now:
        now = _now_pt_str()

    skill = client._skill("reply_intent_parser")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    user_payload = {
        "inbound_text": inbound_text,
        "now": now,
        "sender": sender,
        "recent_outbound": [
            {"kind": r.get("kind"), "text": (r.get("text") or "")[:400]}
            for r in (recent_outbound or [])[-5:]
        ],
        "recent_inbound": [str(s)[:400] for s in (recent_inbound or [])[-5:]],
        "pending_questions": [
            {
                "id": q.get("id"),
                "task_id": q.get("task_id"),
                "task_title_rendered": q.get("task_title_rendered", ""),
                "source_subject": q.get("source_subject", ""),
            }
            for q in pending_questions
        ],
        "pending_facts": [
            {"topic": f.get("topic", ""), "text": (f.get("text") or f.get("snippet") or "")[:400]}
            for f in (pending_facts or [])
        ],
        "open_tasks": [
            {"id": t.get("id"), "title": t.get("title", "")}
            for t in open_tasks
        ],
        "open_coordination_sessions": [
            {
                "session_id": s.get("session_id"),
                "addressee": s.get("addressee") or s.get("addressee_name", ""),
                "ask": s.get("ask") or s.get("coordination_ask", ""),
            }
            for s in (open_coordination_sessions or [])
        ],
    }

    base_msg = (
        "Parse this inbound per the skill procedure. Return ONLY one JSON "
        "object {\"intents\": [...]} with the canonical types. No prose.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )
    # The retry must never push the model toward guessing targets: on
    # 2026-09-25 a strict "targets" retry turned a correct-but-truncated
    # "clarify" into closing three unrelated tasks.
    retry_msg = (
        "Your previous output did not parse. Return STRICTLY one JSON object "
        "of the exact shape {\"intents\": [{\"type\": ..., \"target_text\": ..., "
        "\"targets\": [{\"id\": ..., \"title\": ...}], \"confidence\": ...}]} "
        "and NOTHING else — no code fences, no commentary. If you are not sure "
        "which tasks the message means, return a single intent of type "
        "\"clarify\" with the candidate tasks in targets. Never guess.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    model = client._model_for_call_type("parse_reply_intents")
    # Bounded: one call + ONE retry (cold-fallback policy).
    for attempt, user_msg in enumerate((base_msg, retry_msg)):
        started = client._log_call_start(
            "parse_reply_intents", model, _est_input_tokens(system, user_msg),
        )
        try:
            resp = client._anthropic.messages.create(
                model=model,
                max_tokens=PARSER_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
        except Exception as e:
            client._log_call_failed("parse_reply_intents", started, e, retries=attempt)
            logger.warning("parse_reply_intents API call failed (attempt %d): %s", attempt, e)
            continue
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
        }
        client._log_call_done("parse_reply_intents", model, started, usage,
                              input_text=user_msg, output_text=text)
        parsed = client._extract_json(text)
        intents = _validate_and_normalize(
            parsed, open_tasks=open_tasks, pending_questions=pending_questions,
        )
        if intents is None:
            logger.warning(
                "parse_reply_intents: unusable shape (attempt %d): %r",
                attempt, text[:300],
            )
            continue
        return _apply_deterministic_rules(
            intents, sender=sender, pending_questions=pending_questions,
            inbound_text=inbound_text,
        )
    return None


__all__ = [
    "parse_reply_intents",
    "CANONICAL_INTENT_TYPES",
    "_validate_and_normalize",
    "_apply_deterministic_rules",
    "_title_owner",
]
