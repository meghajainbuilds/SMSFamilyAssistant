"""Capability function: match_target_to_open_task.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/claude_client.py.
Each function takes `client: ClaudeClient` as first arg (replacing `self`).
The ClaudeClient resolves these via __getattr__ so legacy callers using
`claude.match_target_to_open_task(...)` keep working.

Voice/persona/structural rules live in the three canonical homes per
HomeOS three-axis rule:
- PERSONA   -> capabilities/kavi-persona.md
- STRUCTURE -> kavi_runtime/structural_checks.py
- BEHAVIOR  -> the skill file at kavi-runtime/skills/<name>.md
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kavi_runtime.claude_client import ClaudeClient

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list, user_msg: str) -> int:
    """Local helper duplicated from claude_client to avoid circular import."""
    total = sum(len(b.get("text", "")) for b in system)
    total += len(user_msg)
    return int(total / 4)


def match_target_to_open_task(
    client,
    target_text: str,
    open_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """LLM-driven match between Megha's natural-language reference and the
    open tasks in the McMullen-Jain Shared list. Replaces the prior
    exact-string-equality lookup so paraphrases like
    "UW Medicine balance ($630)" can match a stored title of
    "MJ Pay UW Medicine overdue balance ($630.00)".

    Returns:
      {
        "match_id": str | None,        # the unambiguous high-confidence pick, or None
        "candidate_task_ids": list[str], # ALL plausible matches the LLM identified, ordered by relevance
        "confidence": "high" | "medium" | "low",
        "reasoning": str,                # one-line audit explanation
        "_usage": {...},                 # token usage for the cost log
      }

    Why both `match_id` and `candidate_task_ids`: the high-confidence
    single-match case sets `match_id` and the runtime PATCHes immediately.
    The multi-match / ambiguous case sets `match_id=None` AND populates
    `candidate_task_ids` with the small subset (typically 2-4) the LLM
    judged plausible. The runtime saves THAT subset into pending state +
    passes it to the clarifying composer, instead of dumping the full 30
    unrelated candidates (the 2026-05-07 Elders' Tea production bug).

    On API error or parse failure: returns match_id=None,
    candidate_task_ids=[], confidence=low so the caller asks Megha a
    clarifying question rather than executing against a fabricated id.
    Failing-safe is the same bias as the action-intent classifier — a
    missed action is recoverable in one round trip; a wrong execution
    destroys trust.

    Prompt caching: the system prompt (the matching rubric) is stable per
    deploy; the open-tasks payload is dynamic per call. The cache prefix is
    the skill prose only, exactly the same shape as classify_action_intent.
    """
    skill = client._skill("action_target_matcher")
    system_text = f"# Skill\n\n{skill}"
    block: dict[str, Any] = {"type": "text", "text": system_text}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    system = [block]

    # Trim each task to the four fields the matcher needs. Skip tasks with
    # no title (Graph rarely returns these but defensive). Cap at 100 tasks
    # to match the open-task fetch ceiling — the prior 30-cap was the same
    # bug class as the 30-task fetch (deterministic narrowing before the
    # LLM matcher), surfaced 2026-05-08 when 32 open tasks fetched but only
    # 30 reached the matcher.
    trimmed: list[dict[str, Any]] = []
    for t in (open_tasks or [])[:100]:
        if not isinstance(t, dict):
            continue
        title = t.get("title") or ""
        if not title:
            continue
        trimmed.append({
            "id": t.get("id"),
            "title": title,
            "status": t.get("status"),
            "recent_activity": t.get("recent_activity"),
        })

    user_payload = {
        "target_text": target_text or "",
        "open_tasks": trimmed,
    }
    user_msg = (
        "Match the target reference against the open tasks per the skill procedure. "
        "Return ONLY a single raw JSON object with fields "
        "{match_id, candidate_task_ids, confidence, reasoning}. "
        "Do NOT wrap the response in markdown code fences. "
        "Do NOT include prose before or after the JSON. "
        "Keep `reasoning` to one short sentence.\n\n"
        f"Input:\n{json.dumps(user_payload, indent=2)}"
    )

    fallback = {
        "match_id": None,
        "candidate_task_ids": [],
        "confidence": "low",
        "reasoning": "fallback (api or parse error)",
        "_usage": None,
    }

    model = client._model_for_call_type("compose_action_target_match")
    started = client._log_call_start(
        "compose_action_target_match", model,
        _est_input_tokens(system, user_msg),
    )
    try:
        # 1500 tokens accommodates a fully-listed candidate set (each MS To Do
        # task id is ~150 chars / ~50 tokens; 6 candidates alone is ~300 tokens
        # before reasoning). The 400 cap clipped responses mid-JSON in 2026-05-08
        # production, surfacing as parse_error → "no Elders' Tea tasks open" lies.
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed("compose_action_target_match", started, e)
        logger.warning("match_target_to_open_task API call failed: %s", e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done("compose_action_target_match", model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("match_target_to_open_task usage: %s n_tasks=%d", usage, len(trimmed))

    parsed = client._extract_json(text)
    if parsed is None or not isinstance(parsed, dict):
        logger.warning("match_target_to_open_task returned non-JSON: %s", text[:300])
        return {**fallback, "_usage": usage, "_error": "parse_error"}

    match_id = parsed.get("match_id")
    if match_id is not None and not isinstance(match_id, str):
        match_id = None
    confidence = parsed.get("confidence", "low")
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    # If match_id is None, force confidence to low — the matcher should not
    # claim "high" when it returned no id. Defensive against malformed output.
    if match_id is None:
        confidence = "low"
    reasoning = parsed.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""

    # Validate match_id corresponds to one of the candidates we passed in;
    # otherwise treat as no match. Stops fabricated ids cold.
    valid_ids = {t["id"] for t in trimmed if t.get("id")}
    if match_id is not None and match_id not in valid_ids:
        logger.warning(
            "match_target_to_open_task: invented match_id=%r not in candidates; treating as no match",
            match_id,
        )
        match_id = None
        confidence = "low"
        reasoning = f"rejected invented id; original reasoning: {reasoning[:120]}"

    # Validate candidate_task_ids: keep order, drop fabrications + dupes.
    # Defense in depth: even though the skill instructs the LLM to only
    # name proposed ids, an invented id slipping through here would break
    # downstream pending_clarification (the resolver's id-validation
    # would catch it later, but it's cleaner to filter at the source).
    raw_candidates = parsed.get("candidate_task_ids") or []
    candidate_ids: list[str] = []
    if isinstance(raw_candidates, list):
        seen: set[str] = set()
        for cid in raw_candidates:
            if not isinstance(cid, str):
                continue
            if cid in seen or cid not in valid_ids:
                if isinstance(cid, str) and cid not in valid_ids:
                    logger.warning(
                        "match_target_to_open_task: dropped invented candidate_id=%r",
                        cid[:24],
                    )
                continue
            candidate_ids.append(cid)
            seen.add(cid)
    # If the matcher produced a high-confidence match_id but didn't
    # populate candidate_task_ids, reflect the singleton in candidates
    # too — so downstream callers always have a uniform list.
    if match_id is not None and match_id not in candidate_ids:
        candidate_ids = [match_id] + candidate_ids

    return {
        "match_id": match_id,
        "candidate_task_ids": candidate_ids,
        "confidence": confidence,
        "reasoning": reasoning[:400],
        "_usage": usage,
    }
