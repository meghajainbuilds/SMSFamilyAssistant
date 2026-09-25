"""inbox-to-task deep verify procedure.

Phase 4 (2026-06-02): physical move out of
`kavi_runtime/synthetic_compose.py`. Function bodies for
`replay_email_classify` and `verify_email_classify_selection` live here.

Two endpoints today:
- `POST /synthetic/compose/inbox-to-task` → `replay_email_classify`.
- `POST /synthetic/verify/inbox-to-task` → `verify_email_classify_selection`.

Gates currently running in verify_email_classify_selection:
- `expected_decision_mismatch`: classifier returned a status different from
  the caller's `expected_decision`.
- `expected_owner_mismatch`: decision is "task" but task.owner != expected.
- `expected_confidence_mismatch`: classifier returned a different confidence.
"""

from __future__ import annotations

import logging
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
    "expected_decision_mismatch",
    "expected_owner_mismatch",
    "expected_confidence_mismatch",
]


def replay_email_classify(
    config: dict[str, Any],
    email_payload: dict[str, Any],
    *,
    thread_messages: list[dict[str, Any]] | None = None,
    applicable_corrections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay the inbox-to-task classifier + composer with a synthetic email
    payload. Returns the LLM decision plus the input payload as evidence for
    the Verifier sub-agent.
    """
    client = ClaudeClient(config)
    _apply_skill_override(client, email_payload)
    result = client.run_email_to_tasks(
        email_payload,
        thread_messages=thread_messages,
        applicable_corrections=applicable_corrections,
    )

    model_routing = config.get("model_routing", {}) or {}
    model = model_routing.get(
        "compose_email_to_tasks_judgment", config["claude"]["model"]
    )

    decision = result.get("status", "unknown")
    logger.info(
        "inbox_to_task.verify.replay_email_classify: decision=%s (model=%s)",
        decision,
        model,
    )

    return {
        "decision": decision,
        "result": result,
        "model": model,
        "input_payload": email_payload,
    }


def verify_email_classify_selection(
    config: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    """Deep verify mode for inbox-to-task. Runs the classifier on the email
    payload and asserts selection-behavior gates on the result.

    Body shape:
      `email` (required) — normalized email dict.
      `thread_messages` (optional) — thread context.
      `applicable_corrections` (optional) — learned-correction context.
      `expected_decision` (optional str, "task" | "skip" | ...).
      `expected_owner` (optional str, "MJ" | "MM").
      `expected_confidence` (optional str, "high" | "medium" | "low").
    """
    email_payload = body.get("email") or {}
    thread_messages = body.get("thread_messages")
    applicable_corrections = body.get("applicable_corrections")
    expected_decision = body.get("expected_decision")
    expected_owner = body.get("expected_owner")
    expected_confidence = body.get("expected_confidence")

    replay = replay_email_classify(
        config,
        email_payload,
        thread_messages=thread_messages,
        applicable_corrections=applicable_corrections,
    )
    decision = replay.get("decision", "unknown")
    result = replay.get("result") or {}

    failures: list[dict[str, str]] = []

    if expected_decision and decision != expected_decision:
        failures.append({
            "gate": "expected_decision_mismatch",
            "detail": f"expected decision={expected_decision!r}, got {decision!r}",
        })

    if expected_owner and decision == "task":
        task_obj = result.get("task") or {}
        actual_owner = task_obj.get("owner")
        if actual_owner != expected_owner:
            failures.append({
                "gate": "expected_owner_mismatch",
                "detail": (
                    f"expected owner={expected_owner!r}, "
                    f"got owner={actual_owner!r}"
                ),
            })

    if expected_confidence:
        # Confidence lives on the nested task object for "task" decisions
        # (skills/email_to_tasks.md output schema) and at the top level for
        # skip records. Caught by the kavi-pipeline matrix's maiden run
        # (2026-06-10): the top-level-only read returned None for every
        # task decision, so this gate could never pass for creates.
        actual_confidence = result.get("confidence")
        if actual_confidence is None and result.get("task"):
            actual_confidence = (result.get("task") or {}).get("confidence")
        if actual_confidence != expected_confidence:
            failures.append({
                "gate": "expected_confidence_mismatch",
                "detail": (
                    f"expected confidence={expected_confidence!r}, "
                    f"got {actual_confidence!r}"
                ),
            })

    verdict = "PASS" if not failures else "FAIL"

    logger.info(
        "inbox_to_task.verify.verify_email_classify_selection: verdict=%s "
        "failures=%d decision=%s (model=%s)",
        verdict, len(failures), decision, replay.get("model", ""),
    )

    return {
        "output": result,
        "verdict": verdict,
        "failures": failures,
        "model": replay.get("model", ""),
        "input_payload": {
            "email": email_payload,
            "expected_decision": expected_decision,
            "expected_owner": expected_owner,
            "expected_confidence": expected_confidence,
        },
    }


__all__ = [
    "replay_email_classify",
    "verify_email_classify_selection",
    "SHAPE_GATES",
    "SELECTION_GATES",
]
