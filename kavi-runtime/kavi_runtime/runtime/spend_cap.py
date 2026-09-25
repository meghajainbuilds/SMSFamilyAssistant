"""Monthly Anthropic spend cap enforcement.

Called from every LLM-bound capability after each Claude call. If monthly
spend crosses the cap and no bypass is active, sets the pause flag and
sends (or queues for the 7am drain) a trip iMessage.

Lives in `runtime/` because the post-call hook is invoked from multiple
capabilities + the scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from kavi_runtime.runtime.guardrails import (
    compute_monthly_spend_usd,
    is_paused,
    is_spend_cap_bypassed,
    log_trip,
    set_paused,
)
from kavi_runtime.runtime.alerts import _send_or_queue_alert
from kavi_runtime.runtime.paths import _runtime_events_path

logger = logging.getLogger(__name__)


def _check_spend_cap_after_call(config: dict, state_path: Path) -> None:
    """Read month-to-date spend from the persisted running-total counter
    (rewired 2026-06-10; formerly summed runs.jsonl, which carried no usage
    rows — the trip below was unreachable). If over the cap and no bypass
    is active, set the pause flag and send (or queue) a trip iMessage."""
    if is_paused(state_path):
        return  # already paused; don't double-trip
    if is_spend_cap_bypassed(state_path):
        return  # Megha resumed earlier this month; don't re-trip until next month

    cap = config.get("guardrails", {}).get("monthly_anthropic_spend_usd_cap", 30)
    runs_path = _runtime_events_path(config)
    spend = compute_monthly_spend_usd(config)
    if spend < cap:
        return

    logger.warning("spend cap tripped: monthly spend $%.4f >= cap $%.2f", spend, cap)
    set_paused(state_path, reason="spend_cap_exceeded", spend_at_trip=spend)
    log_trip(
        runs_path,
        guardrail="monthly_spend_cap",
        threshold=cap,
        actual=round(spend, 4),
        action="paused_auto_runs",
        extra={"month": datetime.now(timezone.utc).strftime("%Y-%m")},
    )
    alert_text = (
        f"I just hit the monthly spend cap (${spend:.2f} on a ${cap:.0f} cap). "
        f"I'm pausing auto-runs and queuing any new email until you tell me to continue. "
        f"Reply 'resume' (or anything that means 'go ahead') and I'll keep going for the rest of the month."
    )
    _send_or_queue_alert(config, state_path, alert_text, kind="spend_cap_trip",
                          extra={"spend_usd": round(spend, 4), "cap_usd": cap})


__all__ = ["_check_spend_cap_after_call"]
