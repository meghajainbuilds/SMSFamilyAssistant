"""inbox-to-task eval logging — writes one row per email decision to
eval-inbox-judgments.jsonl. Schema per evals/definitions.md (decision-as-
unit). Failure to write is non-fatal.

Phase 4 (2026-06-02): physical move out of `kavi_runtime/handlers.py`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kavi_runtime.state import append_run, utc_now_iso
from kavi_runtime.runtime.ids import _decision_id

logger = logging.getLogger(__name__)


_DECISION_MAP = {
    "task": "created",
    "skipped": "skipped",
    "dedup_semantic": "dedup_hit",
    "paused_skipped": "paused",
    "token_blowup": "token_blowup",
    "lifecycle_updated": "updated",
    "webhook_redup_hit": "webhook_redup_hit",
}


def _eval_inbox_judgments_path(config: dict) -> Path:
    """Eval surface for inbox-to-task. One row per email decision."""
    return Path(config["paths"]["eval_inbox_judgments_jsonl"])


def _log_email_event(config: dict, *, email_payload: dict | None = None, **fields) -> None:
    """Append one row to eval-inbox-judgments.jsonl per email Kavi processed."""
    status = fields.pop("status", None)
    decision = _DECISION_MAP.get(status, status or "unknown")

    email_id = fields.pop("message_id", None)
    if not email_id and email_payload:
        email_id = email_payload.get("id")

    sender = fields.pop("sender", None)
    if not sender and email_payload:
        sender = email_payload.get("from_address") or email_payload.get("from_name")

    subject = fields.pop("subject", None)
    if not subject and email_payload:
        subject = email_payload.get("subject", "")
    if subject and len(subject) > 200:
        subject = subject[:200]

    reason = (
        fields.pop("skip_reason", None)
        or fields.pop("dedup_reason", None)
        or fields.pop("paused_reason", None)
    )

    task_id = fields.pop("task_id", None) or fields.pop("matches_task_id", None)

    package_id = fields.pop("package_id", None)
    package_match_tier = fields.pop("package_match_tier", None) or "none"
    merge_target_task_id = fields.pop("merge_target_task_id", None)
    merge_reason = fields.pop("merge_reason", None)
    lifecycle_state = fields.pop("lifecycle_state", None)
    task_body_length = fields.pop("task_body_length", None)
    source_account = fields.pop("source_account", None)
    matched_task_title = fields.pop("matched_task_title", None)
    matched_task_id = fields.pop("matched_task_id", None)

    record = {
        "decision_id": _decision_id(email_id),
        "ts": utc_now_iso(),
        "capability": "inbox-to-task",
        "email_id": email_id,
        "sender": sender,
        "subject": subject,
        "decision": decision,
        "reason": reason,
        "confidence": fields.pop("confidence", None),
        "task_id": task_id,
        "task_title": fields.pop("title", None),
        "task_owner": fields.pop("owner", None),
        "imessage_sent": fields.pop("imessage_sent", None),
        "latency_first_action_sec": fields.pop("latency_first_action_sec", None),
        "usage": fields.pop("usage", None),
        "package_id": package_id,
        "package_match_tier": package_match_tier,
        "merge_target_task_id": merge_target_task_id,
        "merge_reason": merge_reason,
        "lifecycle_state": lifecycle_state,
        "task_body_length": task_body_length,
        "source_account": source_account,
        "matched_task_title": matched_task_title,
        "matched_task_id": matched_task_id,
    }
    if fields:
        record["extras"] = fields

    try:
        append_run(_eval_inbox_judgments_path(config), record)
    except Exception as e:
        logger.warning("eval-inbox-judgments append failed: %s", e)


def _log_webhook_redup_hit(
    config: dict,
    *,
    email_payload: dict | None = None,
    message_id: str,
    prior_outcome: dict,
    branch: str,
    source_account: str | None = None,
) -> None:
    """Append a `webhook_redup_hit` row when MS Graph re-fires a webhook
    for an email we already decided on inside the TTL window."""
    try:
        prior_decision = prior_outcome.get("decision") if isinstance(prior_outcome, dict) else None
        prior_task_id = prior_outcome.get("task_id") if isinstance(prior_outcome, dict) else None
        prior_decision_id = prior_outcome.get("decision_id") if isinstance(prior_outcome, dict) else None
        _log_email_event(
            config,
            email_payload=email_payload,
            message_id=message_id,
            subject=(email_payload.get("subject", "") if email_payload else "")[:200],
            status="webhook_redup_hit",
            skip_reason=f"webhook_re_fire_within_ttl prior_decision={prior_decision} branch={branch}",
            task_id=prior_task_id,
            suppressed=True,
            prior_decision=prior_decision,
            prior_decision_id=prior_decision_id,
            source_account=source_account,
        )
    except Exception as e:
        logger.warning("webhook_redup_hit log failed (non-fatal): %s", e)


__all__ = [
    "_DECISION_MAP",
    "_eval_inbox_judgments_path",
    "_log_email_event",
    "_log_webhook_redup_hit",
]
