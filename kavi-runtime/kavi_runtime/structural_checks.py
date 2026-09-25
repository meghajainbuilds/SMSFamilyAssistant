"""Structural acceptance-criteria checks for Kavi outbound messages.

Each gate corresponds to an acceptance criterion in capabilities/kavi-persona.md
Metrics → categories B (Voice rules) and C (Patterns to avoid). Functions return
True when the gate PASSES (no violation), False when it fails. structural_check()
runs all checks and returns a dict bundled into the outbound judgment row.

Run on every Kavi outbound at compose time. Failure is signal, not a block —
the message still sends; the eval surface aggregates pass-rates weekly.

G-A1 (action-claim grounding, added 2026-05-07): the only gate in this module
that conditions on `context` rather than text alone. When the outbound text
contains an action verb (sent, marked, added, ...), G-A1 PASSES iff the
context shows a verified tool result (an `actions_executed` entry with
`result=success`) OR the row's `context.tool_grounded` is set True by the
deterministic code path that produced it. This is the Layer 2 defense for
Principle 7.
"""

from __future__ import annotations

import re
from typing import Any

# Allowlist for G-V3. Functional only: low-confidence flag + open question.
ALLOWED_EMOJIS = {"⚠️", "🤔"}

# Common casual emojis to detect violations explicitly. Anything matching the
# emoji unicode ranges that ISN'T in ALLOWED_EMOJIS counts as a violation.
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "☀-⛿"
    "✀-➿"
    "]+",
    flags=re.UNICODE,
)

# G-V2: list markers at line start.
LIST_MARKER_PATTERN = re.compile(r"^\s*([-*•]|\d+\.)\s+", re.MULTILINE)

# G-V4: third-person Kavi-as-subject. Allow "I'm Kavi" (identity statement).
THIRD_PERSON_PATTERN = re.compile(r"\bKavi\s+(noticed|sent|saw|added|wrote|created|did|will|is|has|tracked|logged)\b", re.IGNORECASE)

# G-V5: signoff patterns at message tail.
SIGNOFF_PATTERN = re.compile(r"(\s|\n)\s*(-K|-Kavi|—K|Kavi$|Regards,?\s*Kavi|Best,\s*Kavi)\s*$", re.IGNORECASE)

# G-P1: status-board / system-y language.
STATUS_BOARD_PATTERNS = [
    re.compile(r"\b(\d+\s+(?:tasks?|emails?|items?)\s+added)\b", re.IGNORECASE),
    re.compile(r"\btask\s+created\b", re.IGNORECASE),
    re.compile(r"\b(?:morning|evening|9pm|7am)\s+rollup\b", re.IGNORECASE),
    re.compile(r"\bsummary\s+tick\b", re.IGNORECASE),
    re.compile(r"\bweekly\s+(?:eval|survey|self-check)\b", re.IGNORECASE),
]

# G-P2: formulaic warmth opener.
WARMTH_OPENER_PATTERNS = [
    re.compile(r"^\s*Hi\s+Megha[!,\.]\s*", re.IGNORECASE),
    re.compile(r"^\s*Hey\s+Megha[!,\.]\s*", re.IGNORECASE),
    re.compile(r"hope\s+you'?re\s+having\s+a\s+(?:great|good|wonderful|nice)", re.IGNORECASE),
    re.compile(r"hope\s+(?:your|the)\s+(?:morning|day|week|weekend|evening)\s+is\s+going", re.IGNORECASE),
]

# G-P3: dead "N yes / N no" reply syntax.
DEAD_REPLY_SYNTAX_PATTERN = re.compile(r"\d+\s*yes(\s|/|,|\s+to|$).*\d+\s*no", re.IGNORECASE | re.DOTALL)

# G-A1 (2026-05-07): action verbs that require a verified tool result. The
# canonical list — used in the kavi_conversation skill ban, the post-action
# composer's groundedness rule, the structural-check filter (this list), AND
# the Friday weekly metric — must stay in sync across all four surfaces. If
# you add a verb here, add it everywhere.
#
# Match policy: case-insensitive, word-boundary. We deliberately match the
# inflected forms shipped by the runtime composers (past tense / present
# participle / plain present). "send" alone is NOT included because the
# conversation composer occasionally uses "send" in a future-intent sense
# ("want me to send him a note?") that doesn't claim a completed action.
ACTION_VERBS_REQUIRING_GROUNDING = (
    "sent",
    "marked",
    "added",
    "dropped",
    "deleted",
    "removed",
    "filed",
    "done",
    "resending",
    "resent",
    "delivered",
    "scheduled",
    "queued",
    "completed",
    "closed",
    # Anita bug 2026-06-01: Kavi narrated "Kept - task is on the list"
    # without grounding. The Q&A reply ack template uses "Kept: {title}"
    # as the canonical verb. Past, present, and present-participle forms.
    "kept",
    "keep",
    "keeping",
)
ACTION_VERB_PATTERN = re.compile(
    r"\b(" + "|".join(ACTION_VERBS_REQUIRING_GROUNDING) + r")\b",
    re.IGNORECASE,
)

# Forbidden state-claim phrases for the clarifying composer (Fix 2,
# 2026-05-08). The composer has no tool result in scope when it fires; any
# of these phrases is a hallucinated past-tense state claim. The
# deterministic post-composer check in `claude_client.compose_action_clarifying_reply`
# rewrites the output to a safe fallback question when any of these match.
# Match policy: case-insensitive substring (no word-boundary requirement —
# these are distinctive multi-word phrases that don't accidentally embed in
# legitimate text).
FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES = (
    "already done",
    "already marked",
    "already completed",
    "marked done",
    "done in the system",
    "completed in the system",
    "nothing left open",
    "nothing left on my end",
    # Added 2026-05-08 second pass after the production trace shipped
    # "the volunteering/food donation decision is already showing completed"
    # — this paraphrase wasn't in the original list. Each new phrasing the
    # composer tries gets added here.
    "showing completed",
    "showing as completed",
    "showing as done",
    "is already closed out",
    "are already closed out",
    "already closed out",
)


def text_contains_forbidden_clarify_state_claim(text: str) -> bool:
    """Return True iff `text` contains any of FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES.
    Used by the clarifying composer's deterministic post-check (Fix 2)."""
    lowered = (text or "").lower()
    for phrase in FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES:
        if phrase in lowered:
            return True
    return False


# Canonical length caps for Kavi's outbound text. Single source of truth.
# 2026-05-29 (Phase 2 of EM-critique refactor): promoted from a hardcoded
# magic number in three places (structural_checks G-V1, composer skill,
# claude_client user_msg) to one module-level constant. Update HERE to
# change the cap; importers reference these symbols, not the literal
# digits.
LENGTH_CAP_TARGET = 120  # G-V1 strict gate; the structural pass criterion
LENGTH_CAP_HARD = 180    # composer-side reject above this (over-length None)

# Coordination addressee messages (Kavi → the other partner) carry slightly
# more context than persona one-liners: attribution + the ask + surfaced
# options. The capability spec grants them double the standard target cap.
# Referenced by capabilities/coordination/verify.py (deep-verify shape gate);
# added 2026-06-10 with the kavi-coordinates Phase 0c closure.
COORDINATION_ADDRESSEE_LENGTH_CAP = 240

# Evening suggest-to-close (added 2026-06-10): at most this many close
# suggestions may be NAMED in one rollup message; any remainder is
# summarized as a might-be-closable count, never enumerated. Format/shape
# constraint, so its canonical home is this module (same contract as the
# length caps above): the composer user_msg in
# capabilities/kavi_persona/composers/periodic_summary.py surfaces the
# value to the LLM by referencing this constant; the skill at
# kavi-runtime/skills/periodic_summary_composer.md talks about the
# behavior and references the constant by NAME only (no literal — the
# composer-skill-isolation contract).
CLOSE_SUGGESTIONS_SURFACE_MAX = 2


def passes_g_v1(text: str) -> bool:
    """G-V1: ≤LENGTH_CAP_TARGET char hard cap. (Composer enforces ≤LENGTH_CAP_HARD; this is the strict gate.)"""
    return len(text) <= LENGTH_CAP_TARGET


def passes_g_v2(text: str) -> bool:
    """G-V2: prose, not lists."""
    return LIST_MARKER_PATTERN.search(text) is None


def passes_g_v3(text: str) -> tuple[bool, list[str]]:
    """G-V3: functional emojis only. Returns (passes, list_of_violating_emojis).

    Iterates emoji-pattern matches and rejects any not in ALLOWED_EMOJIS.
    """
    violations: list[str] = []
    for match in EMOJI_PATTERN.finditer(text):
        emoji = match.group(0)
        # Some emojis include variation selectors (U+FE0F); strip for comparison.
        normalized = emoji.replace("️", "")
        if emoji not in ALLOWED_EMOJIS and normalized not in {e.replace("️", "") for e in ALLOWED_EMOJIS}:
            violations.append(emoji)
    return (len(violations) == 0, violations)


def passes_g_v4(text: str) -> bool:
    """G-V4: first-person. No 'Kavi <verb>' patterns. ('I'm Kavi' is fine.)"""
    return THIRD_PERSON_PATTERN.search(text) is None


def passes_g_v5(text: str) -> bool:
    """G-V5: no signoff at tail."""
    return SIGNOFF_PATTERN.search(text) is None


def passes_g_p1(text: str) -> bool:
    """G-P1: no status-board language."""
    return not any(p.search(text) for p in STATUS_BOARD_PATTERNS)


def passes_g_p2(text: str) -> bool:
    """G-P2: no formulaic warmth openers."""
    return not any(p.search(text) for p in WARMTH_OPENER_PATTERNS)


def passes_g_p3(text: str) -> bool:
    """G-P3: no dead 'N yes / N no' reply syntax."""
    return DEAD_REPLY_SYNTAX_PATTERN.search(text) is None


def text_contains_action_verb(text: str) -> bool:
    """Return True iff the text contains any verb from
    ACTION_VERBS_REQUIRING_GROUNDING (case-insensitive, word-boundary).
    Exposed so the Friday metric and the alert-fallback path can share the
    exact same matcher G-A1 uses."""
    return ACTION_VERB_PATTERN.search(text) is not None


def passes_g_a1(text: str, context: dict[str, Any] | None) -> bool:
    """G-A1 (action-claim grounding, added 2026-05-07; refined 2026-05-08): every
    action verb in the outbound text must trace to a verified tool attempt.

    PASSES when ANY of these is true:
      - text contains no action verb from ACTION_VERBS_REQUIRING_GROUNDING.
      - context.tool_grounded is True (the deterministic code path that
        produced this row already verified the Graph call succeeded —
        e.g., qa_ack "Kept: X" / "Dropped: X", correction_ack).
      - context.actions_executed contains at least one entry — meaning the
        action layer attempted an action against MS Graph, and the reply
        text is downstream of that attempt. This includes result=success,
        result=failure (honest failure ack like "couldn't mark any of the 2
        done"), and result=already_completed (verified state read).

    FAILS otherwise: an action verb shipped with NO action attempted. The
    Layer 2 defense against the Principle 7 hallucination class — the
    Elders' Tea trace where compose_action_clarifying_reply fabricated
    "All Elders' Tea tasks are already marked done" with actions_executed=[]
    is exactly this case.

    Refinement note (2026-05-08): the original gate required result=success.
    That conflated "no success" with "hallucination" and caught honest
    failure acks ("Couldn't mark any of the 2 done") as violations. The new
    rule keys on attempt, not outcome — the runtime's structural job is to
    catch claims with no attempt behind them; the LLM-prompt rules in Fix 2
    catch the smaller class of attempted-but-misreported claims.
    """
    if not text_contains_action_verb(text):
        return True
    ctx = context or {}
    if ctx.get("tool_grounded") is True:
        return True
    actions = ctx.get("actions_executed") or []
    if isinstance(actions, list) and actions:
        for a in actions:
            if isinstance(a, dict) and a.get("result"):
                return True
    return False


def structural_check(
    text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run all structural gates on text. Returns a dict of pass/fail + violations.

    Stored on every outbound judgment row. The weekly HTML-viewer open coding
    surfaces these gates per row over a rolling 7-day window.

    `context` is the outbound row's context dict (`actions_executed`,
    `tool_grounded`, etc.). Optional for backward compat — when None, G-A1
    treats no-context as "no grounding" and fails any action-verb-bearing
    text. Pass the dict when you have one (the standard outbound logger does).
    """
    g_v3_pass, g_v3_violations = passes_g_v3(text)
    g_a1_pass = passes_g_a1(text, context)
    return {
        "char_count": len(text),
        "g_v1_120char": passes_g_v1(text),
        "g_v2_prose": passes_g_v2(text),
        "g_v3_functional_emojis": g_v3_pass,
        "g_v3_violations": g_v3_violations,
        "g_v4_first_person": passes_g_v4(text),
        "g_v5_no_signoff": passes_g_v5(text),
        "g_p1_no_status_board": passes_g_p1(text),
        "g_p2_no_warmth_opener": passes_g_p2(text),
        "g_p3_no_dead_reply_syntax": passes_g_p3(text),
        "g_a1_action_grounded": g_a1_pass,
    }


def compute_action_claim_correspondence_rate(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute action_claim_correspondence_rate over a list of outbound rows.

    Used by the weekly HTML-viewer open coding to produce the Friday rollup. Each row is
    a JSONL entry from `eval-persona-outbound-judgments.jsonl`; the
    function reads `text` and `structural_checks.g_a1_action_grounded`
    from each row.

    Returns:
      {
        "rate": float | None,    # None when denominator is 0 ("n/a")
        "verb_rows_count": int,
        "grounded_rows_count": int,
        "ungrounded_rows": [
            {"decision_id": str, "ts": str, "kind": str, "text_snippet": str},
            ...
        ],
      }

    Threshold: 100% (hard). Below 100%, every entry in `ungrounded_rows` is
    a Principle 7 violation.
    """
    verb_rows: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        text = r.get("text") or ""
        if text_contains_action_verb(text):
            verb_rows.append(r)

    grounded: list[dict[str, Any]] = []
    ungrounded: list[dict[str, Any]] = []
    for r in verb_rows:
        checks = r.get("structural_checks") or {}
        if checks.get("g_a1_action_grounded") is True:
            grounded.append(r)
        else:
            ungrounded.append({
                "decision_id": r.get("decision_id", ""),
                "ts": r.get("ts", ""),
                "kind": r.get("kind", ""),
                "text_snippet": (r.get("text") or "")[:80],
            })

    if not verb_rows:
        rate: float | None = None
    else:
        rate = len(grounded) / len(verb_rows)

    return {
        "rate": rate,
        "verb_rows_count": len(verb_rows),
        "grounded_rows_count": len(grounded),
        "ungrounded_rows": ungrounded,
    }


def all_structural_pass(checks: dict[str, Any]) -> bool:
    """Aggregate: did all hard structural gates pass? Used for fast triage in
    aggregator without scanning each gate. Excludes char_count (not a gate)
    and g_v3_violations (already reflected in g_v3_functional_emojis)."""
    boolean_keys = [
        "g_v1_120char",
        "g_v2_prose",
        "g_v3_functional_emojis",
        "g_v4_first_person",
        "g_v5_no_signoff",
        "g_p1_no_status_board",
        "g_p2_no_warmth_opener",
        "g_p3_no_dead_reply_syntax",
        "g_a1_action_grounded",
    ]
    return all(checks.get(k, False) for k in boolean_keys)
