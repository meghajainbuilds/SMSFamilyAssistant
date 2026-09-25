"""Send-or-queue alert helper. Quiet-hours-aware.

Alerts go out as standalone messages, NOT bundled into the digest. Sends
immediately if not in quiet hours; otherwise queues for the 7 AM drain.

Lives in `runtime/` because spend-cap, error-budget, and runtime-health
all need to fire alerts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kavi_runtime.runtime.guardrails import enqueue_pending_alert
from kavi_runtime.state import is_quiet_hours
from kavi_runtime.runtime.send_imessage import _send_imessage_with_fallback

logger = logging.getLogger(__name__)


def _send_or_queue_alert(
    config: dict,
    state_path: Path,
    text: str,
    kind: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Send an alert iMessage now if not in quiet hours; otherwise queue it for the
    7 AM drain. Alerts go out as standalone messages, NOT bundled into the digest."""
    record = {"text": text, "kind": kind, **(extra or {})}
    if is_quiet_hours(config):
        enqueue_pending_alert(state_path, record)
        logger.info("alert queued for 7 AM (quiet hours): kind=%s", kind)
        return
    # Provenance: ops alerts are deterministic text BY SPEC (kavi-persona.md
    # Out of scope: actionable ops alerts use deterministic text, never LLM).
    # AUDIT 2026-06-10: alert texts are honest, actionable, fire-once.
    send_result = _send_imessage_with_fallback(
        config, text, kind=f"alert_{kind}",
        provenance={"fallback_audit": "2026-06-10"},
    )
    if not send_result["verified"] and not send_result["fallback_used"]:
        logger.warning("alert send + outlook fallback both failed; queuing for retry: kind=%s", kind)
        enqueue_pending_alert(state_path, record)
    else:
        logger.info("alert sent: kind=%s verified=%s fallback=%s",
                    kind, send_result["verified"], send_result["fallback_used"])


__all__ = ["_send_or_queue_alert"]
