"""inbox-to-task compose layer — the LLM classifier + composer.

Phase 4 (2026-06-02): physical move. The function body for
`run_email_to_tasks` lives here, not behind a re-export of
`kavi_runtime/claude_client.py`. The `ClaudeClient.run_email_to_tasks`
method on the client class is now a thin proxy that delegates to
this function (preserves callsite compatibility for handlers + tests).

PERSONA: kavi_persona (loaded via persona_loader inside `_build_system_prompt`).
STRUCTURAL CONSTRAINTS: JSON-shape gates + token-cap enforcement in this
function (the post-compose check). Length caps for outbound text don't
apply here — this composer's output is JSON, not Kavi-voiced text.
BEHAVIOR: capabilities/inbox_to_task/skill.md.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list[dict[str, Any]], user_msg: str) -> int:
    """Cheap char/4 estimate over the prompt for the call_start log row."""
    try:
        system_chars = sum(len(b.get("text", "")) for b in system if isinstance(b, dict))
    except Exception:
        system_chars = 0
    return (system_chars + len(user_msg or "")) // 4


# Structured output (2026-09-24). The API guarantees the reply is one object
# of this shape, so the judge can no longer reason in prose before the JSON,
# run into the token cap, and have the email silently logged as skipped
# (about 1% of emails since June; see capabilities/inbox-to-task.md
# changelog 2026-09-24). Mirrors the two shapes in skills/email_to_tasks.md
# as one flat object: task fields are null on a skip and vice versa.
# `reason` comes FIRST: the model writes fields in schema order, so it must
# reason before it commits to `status`. With status first, staging showed it
# lock in "skipped" on the ChatGPT OAuth alert and then argue for "task"
# inside the reason. `reason` is therefore required on both shapes.
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
JUDGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "status": {"type": "string", "enum": ["task", "skipped"]},
        "task": {"anyOf": [{"type": "null"}, {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due": _NULLABLE_STR,
                "owner": {"type": "string", "enum": ["megha", "max", "unassigned"]},
                "owner_reason": {"type": "string"},
                "source_email_id": {"type": "string"},
                "source_subject": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["title", "due", "owner", "owner_reason", "source_email_id",
                         "source_subject", "confidence"],
            "additionalProperties": False,
        }]},
        "email_id": _NULLABLE_STR,
        "subject": _NULLABLE_STR,
    },
    "required": ["reason", "status", "task", "email_id", "subject"],
    "additionalProperties": False,
}

_RETRY_ANCHOR = (
    "\n\nKeep `reason` and `owner_reason` to one short sentence each."
)


def _judgment_failure(parsed: dict[str, Any] | None, stop_reason: str | None) -> str | None:
    """Return why a judge reply can't be trusted, or None if it can.

    A reply cut off by the token cap is never a decision, even if some prefix
    of it parses (the 2026-09-23 ChatGPT alert parsed as `skipped` while the
    model's final, truncated answer was `task`).
    """
    if stop_reason == "max_tokens":
        return "truncated"
    if stop_reason == "refusal":
        return "refusal"
    if parsed is None:
        return "non_json"
    if parsed.get("status") == "task" and not isinstance(parsed.get("task"), dict):
        return "task_missing"
    if parsed.get("status") not in ("task", "skipped"):
        return "bad_status"
    return None


def run_email_to_tasks(
    client: "ClaudeClient",  # type: ignore[name-defined]
    email_payload: dict[str, Any],
    thread_messages: list[dict[str, Any]] | None = None,
    applicable_corrections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calls Anthropic with email_to_tasks system prompt.

    Returns parsed JSON output (one of `task` / `skipped` shapes per skill spec).
    Falls back to a `skipped` record with `reason="parse_error"` if the model
    returns non-JSON.
    """
    # 1-hour cache TTL on the system prefix (added 2026-05-06 per audits/
    # token_optimization_2026-05-06.md REC-2).
    # Behavior section + security layer only; no persona (JSON judge, never
    # speaks in Kavi's voice). 2026-09-23 cost pass: ~37k -> ~14k token prefix.
    system = client._build_system_prompt(
        "email_to_tasks", cache_ttl="1h",
        with_persona=False, with_capability_doc=True,
    )

    source_account = (email_payload or {}).get("source_account")
    from kavi_runtime import household
    if source_account in household.member_emails("max"):
        inbox_owner_default = "max"
    else:
        inbox_owner_default = "megha"

    # Strip internetMessageHeaders from the LLM payload (consumed by inbox pre-filter).
    if isinstance(email_payload, dict) and "internetMessageHeaders" in email_payload:
        email_for_llm = {k: v for k, v in email_payload.items() if k != "internetMessageHeaders"}
    else:
        email_for_llm = email_payload

    user_payload = {
        "email": email_for_llm,
        "thread_messages": thread_messages or [],
        "applicable_corrections": applicable_corrections or [],
        "inbox_owner_default": inbox_owner_default,
    }
    user_msg = (
        "Decide on this email per the skill procedure. Return ONLY a single JSON object "
        "matching one of the two output shapes. No prose around it. The field "
        "`inbox_owner_default` indicates which household member's inbox received this "
        "email; use it as the default owner when the body and recipients give no "
        "clearer signal, but override when content clearly assigns the work to the "
        "other partner.\n\n"
        f"```json\n{json.dumps(user_payload, indent=2)}\n```"
    )

    # Step 12 guardrail: per-call input-token cap.
    system_chars = sum(len(b.get("text", "")) for b in system if isinstance(b, dict))
    estimated = (system_chars + len(user_msg)) // 4
    if estimated > client._per_call_input_token_cap:
        logger.warning(
            "email_to_tasks: pre-call token cap exceeded estimated=%d cap=%d subject=%r",
            estimated, client._per_call_input_token_cap, email_payload.get("subject", "")[:60],
        )
        return {
            "status": "skipped",
            "reason": "token_cap_exceeded",
            "estimated_input_tokens": estimated,
            "input_token_cap": client._per_call_input_token_cap,
            "email_id": email_payload.get("id", "?"),
            "subject": email_payload.get("subject", "?"),
            "_usage": None,
        }

    model = client._model_for_call_type("compose_email_to_tasks_judgment")
    max_toks = client._max_tokens_for_call_type("compose_email_to_tasks_judgment")

    # At most two attempts (cold-fallback policy rule 1: one bounded retry
    # with a tighter prompt). Usage is summed so the spend counter sees both.
    usage: dict[str, int] = {}
    failure: str | None = None
    for attempt in (1, 2):
        attempt_msg = user_msg if attempt == 1 else user_msg + _RETRY_ANCHOR
        started = client._log_call_start(
            "compose_email_to_tasks_judgment", model,
            _est_input_tokens(system, attempt_msg),
        )
        try:
            resp = client._anthropic.messages.create(
                model=model,
                max_tokens=max_toks,
                system=system,
                messages=[{"role": "user", "content": attempt_msg}],
                output_config={"format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA}},
            )
        except Exception as e:
            client._log_call_failed("compose_email_to_tasks_judgment", started, e)
            raise

        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        stop_reason = getattr(resp, "stop_reason", None)
        attempt_usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            # 1h-TTL writes bill at 2x input; carried so the spend counter prices
            # them honestly (2026-09-23).
            "cache_creation_1h_input_tokens": getattr(
                getattr(resp.usage, "cache_creation", None), "ephemeral_1h_input_tokens", 0,
            ) or 0,
        }
        client._log_call_done("compose_email_to_tasks_judgment", model, started,
                              {**attempt_usage, "stop_reason": stop_reason},
                              input_text=attempt_msg, output_text=text)
        for k, v in attempt_usage.items():
            usage[k] = usage.get(k, 0) + v
        usage["stop_reason"] = stop_reason
        usage["attempts"] = attempt
        logger.info("email_to_tasks usage: %s", usage)

        parsed = client._extract_json(text)
        failure = _judgment_failure(parsed, stop_reason)
        if failure is None:
            # Drop the schema's null placeholders so callers see the same two
            # shapes the skill documents.
            result = {k: v for k, v in parsed.items() if v is not None}
            result["_usage"] = usage
            return result
        logger.warning("email_to_tasks attempt %d unusable (%s): %s",
                       attempt, failure, text[:300])

    # Both attempts failed. A missed task is not recoverable and a stray task
    # is (skill conservatism principle), so surface a low-confidence task
    # instead of a silent skip. Default set 2026-09-24; Megha may switch it
    # to an alert text instead.
    # AUDIT 2026-09-24: safe sentence only; no action claims, no enumeration.
    subject = (email_payload or {}).get("subject") or "(no subject)"
    return {
        "status": "task",
        "task": {
            "title": f"Check email Kavi couldn't sort: {subject}"[:80],
            "due": None,
            "owner": inbox_owner_default,
            "owner_reason": f"Judge failed twice ({failure}); defaulted to inbox owner.",
            "source_email_id": (email_payload or {}).get("id", "?"),
            "source_subject": subject,
            "confidence": "low",
        },
        "judge_failure": failure,
        "_usage": usage,
    }


__all__ = ["run_email_to_tasks"]
