"""Outbound message logger — eval surface for kavi-persona generative outputs.

Every Kavi outbound (iMessage, Outlook fallback) gets one append-only row in
eval-persona-outbound-judgments.jsonl. Schema mirrors eval-inbox-judgments.jsonl
shape (one row per "decision," joinable to labels via decision_id).

Structural acceptance gates (G-V1-V5, G-P1-P3) are computed at log time via
structural_checks.structural_check() and stored on the row, so the weekly
HTML-viewer open coding can compute pass-rates without re-parsing.

Glean-rubric gates (G-Gen1-4, G-Ch1-3, G-V6, G-R3) are filled in by Megha's
weekly open coding in the HTML viewer, joined to this file's rows by
decision_id (the chat-based daily slash command surface retired 2026-05-27).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.inbound_log import current_inbound_id
from kavi_runtime.state import append_jsonl
from kavi_runtime.structural_checks import all_structural_pass, structural_check

logger = logging.getLogger(__name__)


def _outbound_path(config: dict) -> Path:
    """Path to eval-persona-outbound-judgments.jsonl on Kavi. Create dir if missing."""
    p = Path(config["paths"]["eval_persona_outbound_judgments_jsonl"])
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _decision_id(kind: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"p_{ts}_{kind}_{uuid.uuid4().hex[:8]}"


def log_outbound(
    config: dict,
    *,
    kind: str,
    text: str,
    send_result: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    recipient: str | None = None,
) -> str:
    """Append one outbound judgment row. Returns decision_id (joinable to labels).

    Args:
      kind: one of {periodic_summary, qa_question, qa_ack, correction_ack,
            self_check, conversational, identity_reply, ack_short, examples_proposal,
            self_check_ack, weekly_self_check, alert, ...}.
            Free-form for forward compat. Use a short stable name.
      text: the verbatim message text sent.
      send_result: output of _send_imessage_with_fallback (verified, fallback_used).
                   Optional; if None, logged as unknown.
      usage: model usage dict {input_tokens, output_tokens, cache_*}. Optional.
      context: free-form dict (task_id, subject, is_rollup, etc.). Optional.
      recipient: who the outbound was addressed to (iMessage handle). Added
                 2026-06-10 with the per-person rollup split so eval rows can
                 distinguish Megha's vs Max's digests. Additive — old rows
                 simply lack the field; never rename existing fields.

    Failure-safe: any exception is logged and swallowed. We never block sending on
    a failure to write the eval surface.
    """
    try:
        decision_id = _decision_id(kind)
        # G-A1 (2026-05-07): pass the outbound's context so action-verb
        # claims can be cross-checked against actions_executed / tool_grounded.
        checks = structural_check(text, context=context)
        record = {
            "decision_id": decision_id,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "capability": "kavi-persona",
            "kind": kind,
            "text": text,
            "char_count": checks.pop("char_count"),
            "structural_checks": checks,
            "structural_pass": all_structural_pass({**checks, "char_count": 0}),
            "fallback_used": (send_result or {}).get("fallback_used", False),
            "verified": (send_result or {}).get("verified", None),
            "recipient": recipient,
            "triggered_by": current_inbound_id.get(),
            "usage": usage,
            "context": context or {},
        }
        append_jsonl(_outbound_path(config), record)
        # Mirror into the active unified trace (Phase B, 2026-05-12). No-op
        # when no Trace context is active. Failure here must not break the
        # send path — same posture as inbound_log.
        try:
            from kavi_runtime.trace_log import current as _trace_current
            _t = _trace_current()
            if _t is not None:
                _t.record_outbound(
                    decision_id=decision_id,
                    kind=kind,
                    text=text,
                    char_count=record["char_count"],
                    structural_checks=record["structural_checks"],
                    structural_pass=record["structural_pass"],
                    fallback_used=record["fallback_used"],
                    verified=record["verified"],
                    context=context or {},
                )
        except Exception as _trace_err:
            logger.debug("outbound_log: trace mirror failed (continuing): %s", _trace_err)
        if not record["structural_pass"]:
            failed = [k for k, v in checks.items() if v is False or (isinstance(v, list) and v)]
            logger.warning("outbound_log: structural fail kind=%s gates=%s text=%r",
                           kind, failed, text[:80])
        return decision_id
    except Exception as e:
        logger.exception("outbound_log: failed to log kind=%s err=%s", kind, e)
        return ""
