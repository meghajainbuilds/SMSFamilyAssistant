"""coordination (kavi-coordinates) deep verify procedure.

Implemented 2026-06-10, closing the Phase 0c gap: kavi-coordinates is
LLM-shaped (`[agentic, two-way, generative]`) and shipped 2026-05-05
without a deep verify entry point. The June 3 incident (deploy restart
orphaned a session; the digest then surfaced "coordinating with Max on
something" for a week) is exactly the selection-behavior class a deep
verify endpoint would have caught on day one.

Two endpoints (wired in capabilities/realtime_kavi/server.py via the
`kavi_runtime/synthetic_compose.py` re-export, same pattern as
kavi-persona):

- `POST /synthetic/compose/kavi-coordinates` → `replay_coordination_addressee`.
  Raw replay of the addressee-message composer (the central user-visible
  LLM output of a coordination session). Lower-level surface for
  Investigators.
- `POST /synthetic/verify/kavi-coordinates` → `verify_coordination_selection`.
  Compose + structured verdict with per-gate failures. The surface
  Verifiers call by default.

Body shape (both endpoints):
```json
{
  "inbound_text": "verbatim requester free-text",
  "requester_name": "Megha",
  "addressee_name": "Max",
  "coordination_ask": "Can Max meet the teachers tomorrow at 3:00 or 3:35?",
  "attribution_judgment": {"should_attribute": true, "reason": "..."}
}
```

Gate categories (tracked by kavi-runtime/tests/test_deep_verify_parity.py):

SHAPE gates (output structure):
- `length_cap` — output exceeds
  `structural_checks.COORDINATION_ADDRESSEE_LENGTH_CAP`.
- `prose_required` — output contains list markers (status-board shape).

SELECTION gates (what the composer surfaced):
- `vague_addressee_message` — output carries NO content keyword from the
  injected session payload's coordination ask. Catches the
  "coordinating with Max on something"-class output where the addressee
  (or requester) is pinged with no actual content.
- `empty_content_outbound` — an empty-content session (blank
  coordination_ask) produced an outbound at all. No content → no send.

Side effects on Kavi state: NONE. Only the composer LLM is called.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.runtime.system_prompt import apply_skill_override as _apply_skill_override
from kavi_runtime.structural_checks import (
    COORDINATION_ADDRESSEE_LENGTH_CAP,
    LIST_MARKER_PATTERN,
)

logger = logging.getLogger(__name__)


# Gate categories tracked for the architectural test
# (kavi-runtime/tests/test_deep_verify_parity.py).
SHAPE_GATES: list[str] = [
    "length_cap",
    "prose_required",
]
SELECTION_GATES: list[str] = [
    "vague_addressee_message",
    "empty_content_outbound",
]


_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]+")

# Words that carry no coordination content even when they appear in the
# ask: household member names (naming Max in a message TO Max is not
# content), generic time words, and filler verbs the intent classifier
# tends to include. A vague output like "can you handle something
# tomorrow?" must NOT pass the content gate on "tomorrow" alone.
_NON_CONTENT_WORDS: set[str] = {
    "megha", "kavi",
    "tomorrow", "today", "tonight", "morning", "afternoon", "evening",
    "week", "weekend",
    "something", "anything", "whether", "about", "with", "check",
    "could", "would", "should", "please", "want", "wants", "asked",
    "have", "has", "does", "need", "needs", "already", "around",
}


def _content_keywords(ask: str, *, addressee_name: str = "") -> list[str]:
    """Pull content-bearing keywords out of a coordination ask. Drops short
    tokens, generic filler, and household member names (including the
    addressee, passed per-session)."""
    drop = set(_NON_CONTENT_WORDS)
    if addressee_name:
        drop.add(addressee_name.strip().lower())
    out: list[str] = []
    for tok in _WORD_TOKEN_RE.findall(ask or ""):
        low = tok.lower()
        if len(low) < 4:
            continue
        if low in drop:
            continue
        out.append(low)
    return out


def _compose_addressee(
    config: dict[str, Any], session_payload: dict[str, Any],
) -> tuple[str | None, str, dict[str, Any]]:
    """Shared compose step for both endpoints. Returns (output_text, model,
    input_payload)."""
    inbound_text = session_payload.get("inbound_text") or ""
    requester_name = session_payload.get("requester_name") or "Megha"
    addressee_name = session_payload.get("addressee_name") or "Max"
    coordination_ask = session_payload.get("coordination_ask") or ""
    attribution_judgment = session_payload.get("attribution_judgment") or {
        "should_attribute": True,
        "reason": "synthetic replay default",
    }

    client = ClaudeClient(config)
    _apply_skill_override(client, session_payload)
    composed = client.compose_coordination_addressee_message(
        inbound_text=inbound_text,
        requester_name=requester_name,
        addressee_name=addressee_name,
        coordination_ask=coordination_ask,
        attribution_judgment=attribution_judgment,
    )
    output = composed.get("text")

    model_routing = config.get("model_routing", {}) or {}
    model = model_routing.get(
        "compose_coordination_addressee_message", config["claude"]["model"]
    )

    input_payload = {
        "inbound_text": inbound_text,
        "requester_name": requester_name,
        "addressee_name": addressee_name,
        "coordination_ask": coordination_ask,
        "attribution_judgment": attribution_judgment,
    }
    return output, model, input_payload


def replay_coordination_addressee(
    config: dict[str, Any],
    session_payload: dict[str, Any],
) -> dict[str, Any]:
    """Replay the coordination addressee-message composer with a synthetic
    session payload. Returns the LLM output plus the input payload as
    evidence for the Verifier sub-agent. Raw surface — no gates; use
    `verify_coordination_selection` for the structured verdict."""
    output, model, input_payload = _compose_addressee(config, session_payload)
    logger.info(
        "coordination.verify.replay_coordination_addressee: composed %d chars (model=%s)",
        len(output or ""), model,
    )
    return {
        "output": output,
        "model": model,
        "input_payload": input_payload,
    }


def verify_coordination_selection(
    config: dict[str, Any],
    session_payload: dict[str, Any],
) -> dict[str, Any]:
    """Deep verify mode for kavi-coordinates. Runs the addressee-message
    composer over the synthetic session payload and asserts both shape and
    selection-behavior gates on the composed output. See module docstring
    for the gate set."""
    output, model, input_payload = _compose_addressee(config, session_payload)
    ask = (input_payload["coordination_ask"] or "").strip()
    addressee_name = input_payload["addressee_name"]

    failures: list[dict[str, str]] = []

    # ---- SELECTION gate: empty-content session must not produce a send ----
    if not ask and output:
        failures.append({
            "gate": "empty_content_outbound",
            "detail": (
                "session payload carries an empty coordination_ask but the "
                "composer produced an outbound; an empty-content session "
                "must refuse (return no text), not ping the addressee with "
                "nothing to say"
            ),
        })

    # ---- SELECTION gate: addressee message must carry the content --------
    if ask and output:
        keywords = _content_keywords(ask, addressee_name=addressee_name)
        if keywords:
            out_lower = output.lower()
            hits = [kw for kw in keywords if kw in out_lower]
            if not hits:
                failures.append({
                    "gate": "vague_addressee_message",
                    "detail": (
                        f"output carries NO content keyword from the "
                        f"coordination ask (expected at least one of "
                        f"{keywords!r}); this is the "
                        f"'coordinating on something'-class vague output — "
                        f"the recipient cannot act on it"
                    ),
                })

    # ---- SHAPE gate: length cap -------------------------------------------
    if output and len(output) > COORDINATION_ADDRESSEE_LENGTH_CAP:
        failures.append({
            "gate": "length_cap",
            "detail": (
                f"output is {len(output)} chars; cap is "
                f"structural_checks.COORDINATION_ADDRESSEE_LENGTH_CAP "
                f"({COORDINATION_ADDRESSEE_LENGTH_CAP})"
            ),
        })

    # ---- SHAPE gate: prose, not lists --------------------------------------
    if output and LIST_MARKER_PATTERN.search(output):
        failures.append({
            "gate": "prose_required",
            "detail": "output contains list markers; iMessage relays are prose",
        })

    verdict = "PASS" if not failures else "FAIL"
    logger.info(
        "coordination.verify.verify_coordination_selection: verdict=%s "
        "failures=%d (model=%s, output=%r)",
        verdict, len(failures), model, (output or "")[:120],
    )
    return {
        "output": output,
        "verdict": verdict,
        "failures": failures,
        "model": model,
        "input_payload": input_payload,
    }


__all__ = [
    "replay_coordination_addressee",
    "verify_coordination_selection",
    "_content_keywords",
    "SHAPE_GATES",
    "SELECTION_GATES",
]
