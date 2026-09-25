"""kavi-persona deep verify: the inbound-reply path (`deep:reply_intent`).

Contract: evals/kavi-reply/matrix/ENDPOINT_CONTRACT.md (authored 2026-06-10
by the matrix author; this module implements it). Two endpoints:

- `POST /synthetic/compose/kavi-reply` -> `replay_kavi_reply` (steps 1-4:
  REAL parser, DRY-RUN executors, REAL reply composer; no gates).
- `POST /synthetic/verify/kavi-reply`  -> `verify_kavi_reply` (adds the
  gate set over parsed intents + dry-run results + composed reply).

Anti-Goodhart shape: these gates live in a DIFFERENT artifact than the
dispatcher under iteration; the frozen matrix lives in evals/ behind a
hash manifest.

Gate categories (mirrors verify.py's SHAPE_GATES / SELECTION_GATES split
for the deep-verify-parity architectural shape):
- shape gates: banned_template_reply, length_cap, prose_required
- selection gates: intent_dropped, wrong_direction_resolution,
  unresolved_context_claim, ungrounded_action_claim
"""

from __future__ import annotations

import logging
import re
from typing import Any

from kavi_runtime.runtime.system_prompt import apply_skill_override as _apply_skill_override

logger = logging.getLogger(__name__)

REPLY_SHAPE_GATES: list[str] = [
    "banned_template_reply",
    "length_cap",
    "prose_required",
]
REPLY_SELECTION_GATES: list[str] = [
    "intent_dropped",
    "wrong_direction_resolution",
    "unresolved_context_claim",
    "ungrounded_action_claim",
]

# ---- request-body schema (contract §1) -------------------------------------

_CONTEXT_FIELDS: dict[str, bool] = {
    # field -> required
    "inbound_text": True,
    "sender": True,
    "recent_outbound": True,
    "recent_inbound": True,
    "pending_questions": True,
    "pending_facts": True,
    "open_tasks": True,
    "open_coordination_sessions": False,
}
_EXPECTATION_FIELDS = ("expected_intents", "forbidden_intents")


def validate_reply_payload(
    payload: Any, *, expectations_required: bool,
) -> str | None:
    """Return None when the payload honors the contract schema; otherwise a
    human-readable rejection reason (the route maps it to 400). Unknown
    fields reject — catches matrix/typo drift early (contract §1)."""
    if not isinstance(payload, dict):
        return "request body must be a JSON object"
    allowed = set(_CONTEXT_FIELDS) | set(_EXPECTATION_FIELDS)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        return f"unknown field(s): {', '.join(unknown)}"
    for field, required in _CONTEXT_FIELDS.items():
        if required and field not in payload:
            return f"missing required field: {field}"
    if payload.get("sender") not in ("megha", "max"):
        return "sender must be 'megha' or 'max'"
    if not isinstance(payload.get("inbound_text"), str) or not payload["inbound_text"].strip():
        return "inbound_text must be a non-empty string"
    for list_field in ("recent_outbound", "recent_inbound", "pending_questions",
                       "pending_facts", "open_tasks"):
        if not isinstance(payload.get(list_field), list):
            return f"{list_field} must be an array"
    if "open_coordination_sessions" in payload and not isinstance(
        payload["open_coordination_sessions"], list,
    ):
        return "open_coordination_sessions must be an array"
    if expectations_required:
        for field in _EXPECTATION_FIELDS:
            if not isinstance(payload.get(field), list):
                return f"{field} must be an array (may be empty)"
            for entry in payload[field]:
                if not isinstance(entry, dict) or "type" not in entry or "target_keyword" not in entry:
                    return f"every {field} entry needs {{type, target_keyword}}"
    return None


# ---- replay (steps 1-4) -----------------------------------------------------


def replay_kavi_reply(
    config: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run the REAL intent parser + DRY-RUN executors + REAL reply composer
    over the synthetic context. No state reads/writes, no Graph calls, no
    sends — the payload is the entire world (contract §2)."""
    from kavi_runtime.claude_client import ClaudeClient
    from capabilities.kavi_persona.intent_executors import execute_intents

    inbound_text = payload["inbound_text"]
    sender = payload["sender"]
    now = payload.get("now")  # deterministic date anchor for date-resolution tests
    recent_outbound = payload.get("recent_outbound") or []
    recent_inbound = payload.get("recent_inbound") or []
    pending_questions = payload.get("pending_questions") or []
    pending_facts = payload.get("pending_facts") or []
    open_tasks = payload.get("open_tasks") or []
    open_sessions = payload.get("open_coordination_sessions") or []

    client = ClaudeClient(config)
    _apply_skill_override(client, payload)
    intents = client.parse_reply_intents(
        inbound_text=inbound_text,
        sender=sender,
        now=now,
        recent_outbound=recent_outbound,
        recent_inbound=recent_inbound,
        pending_questions=pending_questions,
        pending_facts=pending_facts,
        open_tasks=open_tasks,
        open_coordination_sessions=open_sessions,
    )
    parser_failed = intents is None
    intents = intents or []

    # DRY-RUN executors: target resolution real, mutations simulated.
    # graph/claude sentinels are None on purpose — any dry-run code path
    # that touches them is a bug and should raise loudly here.
    executed = execute_intents(
        intents,
        config=config,
        graph=None,
        claude=None,
        list_id="dry-run",
        sender=sender,
        sender_handle=None,
        pending_questions=pending_questions,
        free_text=inbound_text,
        dry_run=True,
    )

    if parser_failed:
        # Production would ship the safe sentence; mirror that here so the
        # verify gates grade what the family would actually see.
        from kavi_runtime.runtime.imessage_dispatch import _REPLY_SAFE_FALLBACK
        output: str | None = _REPLY_SAFE_FALLBACK
    else:
        output = client.compose_kavi_reply(
            inbound_text=inbound_text,
            sender=sender,
            intents=intents,
            executed=executed,
            recent_outbound=recent_outbound,
            recent_inbound=recent_inbound,
            pending_facts=pending_facts,
        )
        if output is None:
            from kavi_runtime.runtime.imessage_dispatch import _REPLY_SAFE_FALLBACK
            output = _REPLY_SAFE_FALLBACK

    model_routing = config.get("model_routing", {}) or {}
    model = model_routing.get(
        "compose_kavi_reply",
        model_routing.get("default", config["claude"]["model"]),
    )

    input_payload = {
        k: payload.get(k)
        for k in _CONTEXT_FIELDS
        if k in payload
    }
    return {
        "output": output,
        "parsed_intents": intents,
        "executed": executed,
        "model": model,
        "input_payload": input_payload,
    }


# ---- gate helpers -----------------------------------------------------------

_BANNED_TEMPLATE_RES: list[re.Pattern[str]] = [
    re.compile(r"^Got it\.$"),
    re.compile(r"^Got it!$"),
    re.compile(r"^Kept: "),
    re.compile(r"^Dropped: "),
]

# Canonical no-context phrase list (contract: "Canonical pattern list lives
# in the verify module (one home, extend there)"). Phrase heuristic; a
# paraphrased amnesia claim can escape it — documented limit.
NO_CONTEXT_PHRASES: tuple[str, ...] = (
    "don't have context",
    "do not have context",
    "no context on",
    "don't have that context",
    "not sure what you're referring to",
)

_NEGATION_TOKENS: tuple[str, ...] = (
    "not", "n't", "never", "no ", "nothing", "haven't", "hasn't", "hadn't",
    "didn't", "couldn't", "can't", "cannot", "won't", "wasn't", "weren't",
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _normalize(text: str) -> str:
    return (text or "").replace("’", "'")


def _expectation_matches_intent(exp: dict[str, Any], intent: dict[str, Any]) -> bool:
    """Contract §3 matching: type equality AND (null keyword, OR keyword
    appears case-insensitively in any resolved target's title or in the
    intent's target_text)."""
    if exp.get("type") != intent.get("type"):
        return False
    kw = exp.get("target_keyword")
    if kw is None:
        return True
    kw_l = str(kw).lower()
    if kw_l in (intent.get("target_text") or "").lower():
        return True
    for t in intent.get("targets") or []:
        if kw_l in (t.get("title") or "").lower():
            return True
    return False


def _detect_ungrounded_action_claims(
    output: str, executed: list[dict[str, Any]],
) -> list[str]:
    """ungrounded_action_claim gate. AFFIRMATIVE (non-negated, non-question)
    action-verb claims need a backing execution result; claims backed by a
    simulated-success result pass — in dry-run the composer is ALLOWED to
    narrate the simulated executions (contract §4). Negation-aware:
    "I haven't sent them" passes. Heuristic limits (documented): grounding
    is message-level (any success-shaped result backs the message's
    claims), not per-verb; question sentences are exempt as non-claims."""
    from kavi_runtime.structural_checks import ACTION_VERB_PATTERN

    if not output:
        return []
    has_backing = any(
        r.get("result") in {"success", "already_completed", "partial_failure"}
        for r in executed or []
    )
    failures: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(_normalize(output)):
        s = sentence.strip()
        if not s or s.endswith("?"):
            continue
        for m in ACTION_VERB_PATTERN.finditer(s):
            prefix = s[: m.start()].lower()
            if any(tok in prefix for tok in _NEGATION_TOKENS):
                continue
            if has_backing:
                continue
            failures.append(
                f"affirmative action claim {m.group(0)!r} in {s!r} with no "
                f"execution result backing it (G-A1 canonical verb list; "
                f"negation-aware)"
            )
    return failures


def run_reply_gates(
    *,
    output: str | None,
    parsed_intents: list[dict[str, Any]],
    executed: list[dict[str, Any]],
    payload: dict[str, Any],
) -> list[dict[str, str]]:
    """Run the full gate set (contract §4). All gates run; each failure adds
    a {gate, detail} row."""
    from kavi_runtime.structural_checks import (
        LENGTH_CAP_TARGET,
        LIST_MARKER_PATTERN,
    )

    failures: list[dict[str, str]] = []
    text = output or ""

    # intent_dropped: an expected entry matched by NO parsed intent.
    for exp in payload.get("expected_intents") or []:
        if not any(_expectation_matches_intent(exp, i) for i in parsed_intents):
            failures.append({
                "gate": "intent_dropped",
                "detail": (
                    f"expected intent {exp!r} matched no parsed intent "
                    f"(parsed types: {[i.get('type') for i in parsed_intents]})"
                ),
            })

    # wrong_direction_resolution: a forbidden entry matched by ANY intent.
    for forb in payload.get("forbidden_intents") or []:
        for intent in parsed_intents:
            if _expectation_matches_intent(forb, intent):
                failures.append({
                    "gate": "wrong_direction_resolution",
                    "detail": (
                        f"forbidden intent {forb!r} matched parsed intent "
                        f"type={intent.get('type')!r} "
                        f"target_text={intent.get('target_text')!r} "
                        f"targets={[t.get('title') for t in intent.get('targets') or []]}"
                    ),
                })

    # banned_template_reply: the dead templates the rebuild kills.
    for pat in _BANNED_TEMPLATE_RES:
        if pat.search(text):
            failures.append({
                "gate": "banned_template_reply",
                "detail": f"reply matches banned template {pat.pattern!r}: {text[:80]!r}",
            })

    # unresolved_context_claim: amnesia claim while her context exists.
    if payload.get("recent_inbound"):
        lowered = _normalize(text).lower()
        for phrase in NO_CONTEXT_PHRASES:
            if phrase in lowered:
                failures.append({
                    "gate": "unresolved_context_claim",
                    "detail": (
                        f"reply claims missing context ({phrase!r}) while "
                        f"recent_inbound is non-empty: {text[:120]!r}"
                    ),
                })

    # ungrounded_action_claim (heuristic, negation-aware).
    for detail in _detect_ungrounded_action_claims(text, executed):
        failures.append({"gate": "ungrounded_action_claim", "detail": detail})

    # length_cap (reference the constant, never the literal).
    if len(text) > LENGTH_CAP_TARGET:
        failures.append({
            "gate": "length_cap",
            "detail": (
                f"reply is {len(text)} chars; cap is "
                f"structural_checks.LENGTH_CAP_TARGET ({LENGTH_CAP_TARGET})"
            ),
        })

    # prose_required (same check as the other verify routes).
    if LIST_MARKER_PATTERN.search(text):
        failures.append({
            "gate": "prose_required",
            "detail": f"reply contains list markers: {text[:120]!r}",
        })

    return failures


def verify_kavi_reply(
    config: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Gated verify mode (contract §5): replay + gate set + verdict."""
    replayed = replay_kavi_reply(config, payload)
    failures = run_reply_gates(
        output=replayed["output"],
        parsed_intents=replayed["parsed_intents"],
        executed=replayed["executed"],
        payload=payload,
    )
    verdict = "PASS" if not failures else "FAIL"
    logger.info(
        "kavi_persona.verify_reply: verdict=%s failures=%d (output=%r)",
        verdict, len(failures), (replayed["output"] or "")[:120],
    )
    return {
        "output": replayed["output"],
        "verdict": verdict,
        "failures": failures,
        "parsed_intents": replayed["parsed_intents"],
        "executed": replayed["executed"],
        "model": replayed["model"],
        "input_payload": replayed["input_payload"],
    }


__all__ = [
    "replay_kavi_reply",
    "verify_kavi_reply",
    "run_reply_gates",
    "validate_reply_payload",
    "NO_CONTEXT_PHRASES",
    "REPLY_SHAPE_GATES",
    "REPLY_SELECTION_GATES",
]
