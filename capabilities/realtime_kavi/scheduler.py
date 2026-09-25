"""Cron-style scheduler. Runs periodic_summary at fixed times and the correction-pattern
check every N hours. is_quiet_hours moved to state.py so handlers.py can use it without
a circular import."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from apscheduler.events import EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from kavi_runtime.runtime_status import record_cron_tick

from kavi_runtime.channel_heartbeat import run_daily_channel_heartbeat
from kavi_runtime.runtime.guardrails import (
    deferred_queue_size,
    drain_pending_alerts,
    pop_deferred,
    webhook_flood_active,
)
from kavi_runtime.handlers import (
    correction_pattern_check,
    email_arrived,
    periodic_summary,
    run_coordination_followup_sweep,
)
from kavi_runtime.runtime.clients import _get_clients
from capabilities.realtime_kavi.subscription_renewal import (
    subscription_renewal_check_all_accounts,
)
from kavi_runtime.error_budget import daily_error_budget_job
from kavi_runtime.log_rotation import nightly_log_rotation_job
from capabilities.realtime_kavi.runtime_health import (
    funnel_public_url,
    funnel_reachability_check,
    healthcheck_url,
    ping_healthcheck,
    runtime_smoke_test,
)
from kavi_runtime.snapshot import nightly_snapshot_job
from kavi_runtime.runtime.weekly_self_check import (
    expire_pending_self_check,
    send_weekly_self_check,
)

logger = logging.getLogger(__name__)


def drain_deferred_event(config: dict) -> None:
    """1-min interval job. Pops one notification from the deferred queue (if any)
    and dispatches it. Continues throttling at 1/min while flood is active and the
    queue still has items.

    Webhook payloads queued during a flood carry a `_source_account` stash
    set by the server-side router. We pass it back into email_arrived so a
    deferred Max email still routes through Max's stream."""
    if deferred_queue_size() == 0:
        return
    n = pop_deferred()
    if n is None:
        return
    source_account = n.pop("_source_account", None) if isinstance(n, dict) else None
    logger.info(
        "deferred_drain: dispatching one (queue_size_remaining=%d, flood_active=%s, account=%s)",
        deferred_queue_size(), webhook_flood_active(config), source_account or "default",
    )
    try:
        email_arrived(n, config, source_account)
    except Exception:
        logger.exception("deferred_drain: email_arrived failed")


def quiet_window_watchdog(config: dict) -> None:
    """5-min interval job (added 2026-07-14 after the 17-day stuck-pause).

    Watches ONLY a user_requested_quiet pause — spend/webhook/api pauses are
    left alone (they must stay down until the underlying condition clears + Megha
    confirms). Two jobs in one, because both are "is the quiet window behaving?":

      1. Auto-resume: once the quiet window (`paused_until`) elapses, resume —
         which drains the queued emails and clears the pause. This is the fix
         for "quiet until tomorrow morning" that used to never end.
      2. Liveness: the backstop for the backstop. If the pause is un-expirable
         (legacy pause with no wake time) or overdue past a grace window (the
         resume job somehow not firing), alert Megha ONCE per episode so a
         silent email->task outage can never again hide behind Kavi's morning
         messages. Silence-is-not-failure still holds: this fires only on a
         genuinely stuck quiet pause, never on a quiet inbox.
    """
    from kavi_runtime.runtime.guardrails import (
        should_auto_resume,
        quiet_pause_stuck_reason,
        get_pause_state,
        load_pause_state,
        save_pause_state,
    )
    from kavi_runtime.runtime.pause import _handle_resume
    from kavi_runtime.runtime.alerts import _send_or_queue_alert

    state_path = Path(config["paths"]["imessage_state"])
    ps = get_pause_state(state_path)
    if not ps["paused"]:
        # Resume an interrupted drain (added 2026-09-23): the queue now stays
        # on disk until each email is handled, so emails left behind by a
        # restart or crash mid-drain are picked up here instead of lost.
        _resume_leftover_drain(config, state_path)
        return
    if ps.get("paused_reason") != "user_requested_quiet":
        # Every other pause reason (spend, webhook, api) gets the same liveness
        # backstop (added 2026-09-23): the Aug 11 spend pause stayed silently
        # stuck 43 days because only quiet pauses were watched.
        _alert_if_long_pause(config, state_path)
        return

    if should_auto_resume(state_path):
        logger.info("quiet_window_watchdog: quiet window elapsed — auto-resuming")
        _handle_resume(state_path, config, {"reason": "auto-resume: quiet window elapsed"})
        return

    stuck_reason = quiet_pause_stuck_reason(state_path)
    if not stuck_reason:
        return
    if load_pause_state(state_path).get("liveness_alert_sent"):
        return  # already alerted this stuck episode; debounce
    queue_len = len(load_pause_state(state_path).get("paused_email_queue", []))
    logger.warning(
        "quiet_window_watchdog: STUCK quiet pause (%s); %d emails queued — alerting Megha",
        stuck_reason, queue_len,
    )
    _send_or_queue_alert(
        config, state_path,
        f"I'm still on quiet mode and {stuck_reason} — {queue_len} email"
        f"{'s' if queue_len != 1 else ''} are waiting and not becoming tasks. "
        f"Reply 'resume' to catch up.",
        kind="stuck_quiet_pause",
    )
    marker = load_pause_state(state_path)
    marker["liveness_alert_sent"] = True
    save_pause_state(state_path, marker)


LONG_PAUSE_ALERT_HOURS = 24


def _resume_leftover_drain(config: dict, state_path: Path) -> None:
    """Drain queued emails left behind while NOT paused (restart mid-drain, or
    failures awaiting retry). Held accounts don't count: they wait for a
    re-auth, so a queue holding only their mail is left alone."""
    from kavi_runtime.runtime.guardrails import load_pause_state
    from kavi_runtime.runtime.pause import drain_paused_queue

    hold = {a.lower() for a in config.get("guardrails", {}).get("drain_hold_accounts", []) or []}
    queue = load_pause_state(state_path).get("paused_email_queue", [])
    pending = [
        n for n in queue
        if not (isinstance(n, dict) and (n.get("_source_account") or "").lower() in hold)
    ]
    if not pending:
        return
    logger.info("pause_watchdog: %d queued emails left while running; resuming drain", len(pending))
    drain_paused_queue(state_path, config)


def _alert_if_long_pause(config: dict, state_path: Path) -> None:
    """Alert Megha ONCE per episode when a non-quiet pause has lasted longer
    than LONG_PAUSE_ALERT_HOURS. Debounced by the same liveness_alert_sent
    marker, which set_paused/clear_paused reset per episode."""
    from datetime import datetime, timedelta, timezone
    from kavi_runtime.runtime.guardrails import load_pause_state, save_pause_state
    from kavi_runtime.runtime.alerts import _send_or_queue_alert

    ps = load_pause_state(state_path)
    if ps.get("liveness_alert_sent"):
        return
    since = ps.get("paused_since")
    try:
        since_dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        since_dt = None  # unknown start: treat as long-running
    now = datetime.now(timezone.utc)
    if since_dt and now - since_dt < timedelta(hours=LONG_PAUSE_ALERT_HOURS):
        return
    reason = {
        "spend_cap_exceeded": "the monthly spend cap",
    }.get(ps.get("paused_reason"), str(ps.get("paused_reason") or "an unknown reason"))
    days = (now - since_dt).days if since_dt else None
    queue_len = len(ps.get("paused_email_queue", []))
    how_long = f"for {days} day{'s' if days != 1 else ''}" if days else "for over a day"
    logger.warning("pause_watchdog: long pause (%s, %s); %d queued — alerting", reason, how_long, queue_len)
    # AUDIT 2026-09-23: deterministic ops alert, grounded counts, no action claims.
    _send_or_queue_alert(
        config, state_path,
        f"Heads up: I've been paused {how_long} ({reason}), so new email isn't "
        f"becoming tasks. {queue_len} waiting. Reply 'resume' to catch up.",
        kind="stuck_pause",
    )
    ps = load_pause_state(state_path)
    ps["liveness_alert_sent"] = True
    save_pause_state(state_path, ps)


def drain_pending_alerts_morning(config: dict) -> None:
    """7 AM job. Drains any guardrail-trip iMessages queued during quiet hours and
    sends each as a standalone message (NOT bundled into the 7 AM digest).

    Each drained alert routes through the canonical send wrapper
    (`handlers._send_imessage_with_fallback`) so the recipient allowlist
    plus content scanner gates run on the queued payload before SEND.
    """
    state_path = Path(config["paths"]["imessage_state"])
    alerts = drain_pending_alerts(state_path)
    if not alerts:
        return
    from kavi_runtime.handlers import _send_imessage_with_fallback
    logger.info("morning alert drain: %d pending", len(alerts))
    for alert in alerts:
        try:
            # AUDIT 2026-06-10: re-send of a queued deterministic ops alert
            # (alerts are deterministic by spec; see runtime/alerts.py).
            _send_imessage_with_fallback(
                config, alert["text"],
                kind=f"alertdrain_{alert.get('kind', 'unknown')}",
                provenance={"fallback_audit": "2026-06-10"},
            )
        except Exception:
            logger.exception("alert send failed for kind=%s", alert.get("kind"))


def bb_heartbeat(config: dict) -> None:
    """Force BlueBubbles to scan chat.db on a fixed cadence. BB's FSEvents-based
    listener can go silent during macOS low-power / standby — once that happens,
    inbound iMessages stack up unprocessed until something wakes BB. A periodic
    /api/v1/message/query call keeps BB's chat.db reads warm and flushes any
    pending backlog. Local HTTP only; no LLM / cloud cost."""
    _, _, bb = _get_clients(config)
    try:
        bb.fetch_recent_messages(limit=1)
    except Exception:
        logger.exception("bb_heartbeat failed")


def start_scheduler(config: dict) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="America/Los_Angeles")

    # STAGING GATE (added 2026-06-10, capability-build pipeline). A staging
    # instance (config.staging_mode true) registers NO scheduled jobs:
    # every job in this module either produces outbound (digests, rollup,
    # self-check, alert drains, channel heartbeat), calls MS Graph
    # (subscription renewal, deferred email drain), or probes production
    # infrastructure (BlueBubbles heartbeat, Funnel checks, healthchecks.io
    # pings, smoke test). None of that belongs on the replay sandbox, and
    # gating registration wholesale is safer than maintaining a per-job
    # allowlist that drifts. The scheduler object still starts so main.py's
    # shutdown wiring is unchanged. See capabilities/BUILD_PIPELINE.md.
    if config.get("staging_mode"):
        scheduler.start()
        logger.warning(
            "STAGING: scheduler started with NO jobs registered "
            "(staging_mode=true; outbound/Graph/probe jobs are disabled)"
        )
        return scheduler

    # Cron-tick tracker (added 2026-05-26 alongside the `/status` HTTP
    # endpoint). Every successful job execution records its job_id in
    # runtime_status so the status page can show "Last cron tick: <job>,
    # <time PT>". One listener catches every job; we never have to wrap
    # individual add_job calls. Failures are swallowed inside
    # record_cron_tick so a tracker bug cannot break job scheduling.
    def _on_job_executed(event):
        try:
            record_cron_tick(event.job_id)
        except Exception:
            logger.debug("scheduler: record_cron_tick failed for %s", event.job_id)

    scheduler.add_listener(_on_job_executed, EVENT_JOB_EXECUTED)

    for hhmm in config["schedule"]["periodic_summary_times"]:
        hour, minute = map(int, hhmm.split(":"))
        scheduler.add_job(
            periodic_summary,
            CronTrigger(hour=hour, minute=minute),
            args=[config, False],
            id=f"summary-{hhmm}",
            replace_existing=True,
        )

    rollup_hh, rollup_mm = map(int, config["schedule"]["end_of_day_rollup"].split(":"))
    scheduler.add_job(
        periodic_summary,
        CronTrigger(hour=rollup_hh, minute=rollup_mm),
        args=[config, True],
        id="rollup",
        replace_existing=True,
    )

    every_hours = config["schedule"]["correction_pattern_check_every_hours"]
    scheduler.add_job(
        correction_pattern_check,
        IntervalTrigger(hours=every_hours),
        args=[config],
        id="correction-pattern",
        replace_existing=True,
    )

    # MS Graph subscription renewal: cadence aligned with the renewal buffer so we
    # always fire well before expiry. The handler iterates every household account
    # and renews each subscription independently. Accounts whose token has not yet
    # been added (no token cache on disk) are skipped with a warning, so a missing
    # account never crashes the scheduler — it stays silent until Megha runs
    # `python -m kavi_runtime.add_account <email>`.
    renewal_minutes = max(15, config["graph"]["subscription_renewal_buffer_minutes"] // 2)
    scheduler.add_job(
        subscription_renewal_check_all_accounts,
        IntervalTrigger(minutes=renewal_minutes),
        args=[config],
        id="graph-subscription-renewal",
        replace_existing=True,
        next_run_time=datetime.now(),  # run once at startup so a stale subscription self-heals
    )

    # Step 12 guardrails (added 2026-04-29):
    # - 1-minute drain of the webhook-flood deferred queue. No-op when empty.
    # - 7:00 AM drain of any guardrail-trip alerts queued during quiet hours.
    scheduler.add_job(
        drain_deferred_event,
        IntervalTrigger(minutes=1),
        args=[config],
        id="webhook-deferred-drain",
        replace_existing=True,
    )
    scheduler.add_job(
        drain_pending_alerts_morning,
        CronTrigger(hour=7, minute=0),
        args=[config],
        id="morning-alerts-drain",
        replace_existing=True,
    )

    # Quiet-window watchdog (added 2026-07-14): auto-resume a user_requested_quiet
    # pause once its window elapses, and alert Megha if such a pause gets stuck.
    # 5-min cadence — a "quiet until morning" resumes within 5 min of its wake
    # time, and a stuck pause is caught within 5 min of going overdue. Runs at
    # startup too so a pause left stuck across a restart self-heals immediately.
    scheduler.add_job(
        quiet_window_watchdog,
        IntervalTrigger(minutes=5),
        args=[config],
        id="quiet-window-watchdog",
        replace_existing=True,
        next_run_time=datetime.now(),
    )

    # BB chat.db watcher heartbeat: 2-minute cadence. See bb_heartbeat docstring.
    scheduler.add_job(
        bb_heartbeat,
        IntervalTrigger(minutes=2),
        args=[config],
        id="bluebubbles-heartbeat",
        replace_existing=True,
    )

    # Coordination follow-up sweep (2026-06-22): every 10 min, check open
    # coordination sessions and tell the REQUESTER "still waiting" at the
    # inferred soft window and "they haven't responded — follow up or leave
    # it?" at the hard window (≤24h). Each checkpoint fires at most once per
    # session; quiet hours are skipped inside the sweep. No-op when no session
    # is awaiting a reply.
    scheduler.add_job(
        run_coordination_followup_sweep,
        IntervalTrigger(minutes=10),
        args=[config],
        id="coordination-followup-sweep",
        replace_existing=True,
    )

    # Daily per-recipient iMessage channel heartbeat (added 2026-06-03 as the
    # SRE safety net for the per-message receipt-as-truth verify rewrite).
    # Sends a tiny ping to each household recipient (Megha, Max today; any
    # future household members the moment they're added to config) and
    # confirms the ping round-trips into the recipient's chat history within
    # an hour. If round-trip fails for >24h on any recipient, emails Megha
    # one alert (dedupe'd until the next successful probe). 08:00 PT so it
    # lands after the morning digest (07:00 PT) but before the work day's
    # high-iMessage-activity window.
    scheduler.add_job(
        run_daily_channel_heartbeat,
        CronTrigger(hour=8, minute=0),
        args=[config],
        id="channel-heartbeat-daily",
        replace_existing=True,
    )

    # Invocation-floor iMessage alarm: REMOVED 2026-05-26. The 60-min
    # "no activity" alert path generated 75 of 89 persona-eval rows and
    # 11 of 84 labeled rubric sessions over 7 days, all false positives.
    # Silence is not failure. Megha now pulls status on demand from the
    # `/status` HTTP endpoint; real silent failures are caught by the
    # external healthchecks.io watchdog (see `healthcheck.url` in
    # config.yaml and the `healthcheck-ping` job below). See
    # kavi-runtime/backlog.md "Runtime heartbeat" entry.

    # Funnel reachability probe: every 15 min, hit the runtime's own public
    # URL and verify the Tailscale Funnel → loopback → runtime path works.
    # Catches today's 2026-05-06 PM root cause class (server.host drift
    # from Funnel forward target). Skipped cleanly when public_url is
    # unconfigured.
    if funnel_public_url(config):
        scheduler.add_job(
            funnel_reachability_check,
            IntervalTrigger(minutes=15),
            args=[config],
            id="funnel-reachability-check",
            replace_existing=True,
            next_run_time=datetime.now(),  # first probe at startup
        )

        # Post-restart E2E smoke test (P1.4): once, ~30 sec after startup,
        # POST a synthetic Graph notification through the public URL and
        # verify the runtime accepts it. Proves the FULL inbound chain
        # (Funnel → loopback → webhook router → handler dispatch) works
        # without waiting for a real email. Skipped cleanly when
        # subscription state is missing.
        from datetime import timedelta as _td
        scheduler.add_job(
            runtime_smoke_test,
            args=[config],
            id="post-restart-smoke-test",
            replace_existing=True,
            next_run_time=datetime.now() + _td(seconds=30),
        )

    # Stage 4 (added 2026-05-04): weekly self-check (cognitive-load direct signal).
    # Friday 14:00 Pacific: send. Saturday 14:00 Pacific (24h after): expire if no reply.
    # Configurable via schedule.weekly_self_check_send_time (default "14:00") and
    # schedule.weekly_self_check_timeout_hours (default 24, drives the Saturday tick).
    sc_time = config.get("schedule", {}).get("weekly_self_check_send_time", "14:00")
    sc_hour, sc_minute = map(int, sc_time.split(":"))
    scheduler.add_job(
        send_weekly_self_check,
        CronTrigger(day_of_week="fri", hour=sc_hour, minute=sc_minute),
        args=[config],
        id="weekly-self-check-send",
        replace_existing=True,
    )
    scheduler.add_job(
        expire_pending_self_check,
        CronTrigger(day_of_week="sat", hour=sc_hour, minute=sc_minute),
        args=[config],
        id="weekly-self-check-expire",
        replace_existing=True,
    )

    # Daily error-budget rollup (added 2026-05-06, item #6 hygiene pass).
    # 7am Pacific Time. Reads the previous 24h of structured JSON log,
    # writes a markdown rollup to runtime_metrics/error_budget_<date>.md,
    # and emails Megha. See kavi_runtime/error_budget.py.
    scheduler.add_job(
        daily_error_budget_job,
        CronTrigger(hour=7, minute=0),
        args=[config],
        id="daily-error-budget",
        replace_existing=True,
    )

    # Nightly snapshot of state files (added 2026-05-06, item #1 hygiene pass).
    # 03:00 Pacific Time. Copies 5 state files to /Users/kavi/kavi-runtime/snapshots/
    # <UTC-date>/ and prunes anything older than 14 days. See kavi_runtime/snapshot.py.
    scheduler.add_job(
        nightly_snapshot_job,
        CronTrigger(hour=3, minute=0),
        args=[config],
        id="nightly-snapshot",
        replace_existing=True,
    )

    # Nightly line-log rotation (added 2026-05-06 batch 6, item #3).
    # 03:30 Pacific Time — staggered after the snapshot job so the two
    # don't compete for filesystem cache. Rotates kavi-runtime.log and
    # kavi-runtime.err.log into dated archives and prunes archives older
    # than 30 days. See kavi_runtime/log_rotation.py.
    scheduler.add_job(
        nightly_log_rotation_job,
        CronTrigger(hour=3, minute=30),
        args=[config],
        id="nightly-log-rotation",
        replace_existing=True,
    )

    # Dead-man's-switch heartbeat to healthchecks.io (added 2026-05-05). 5-min
    # cadence pairs with a healthchecks.io check configured period=5min +
    # grace=15min, so a stalled runtime triggers an email to Megha within ~15
    # min of going silent. See kavi_runtime/runtime_health.py.
    if healthcheck_url(config):
        scheduler.add_job(
            ping_healthcheck,
            IntervalTrigger(minutes=5),
            args=[config],
            id="healthcheck-ping",
            replace_existing=True,
        )
    else:
        logger.debug("healthcheck disabled (config.healthcheck.url not set)")

    scheduler.start()
    logger.info("scheduler started; quiet hours %s-%s",
                config["schedule"]["quiet_hours_start"],
                config["schedule"]["quiet_hours_end"])
    return scheduler
