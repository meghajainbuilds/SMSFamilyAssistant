"""Error-budget tracking (item #6 of 2026-05-06 hygiene pass).

Reads the JSON log produced by `kavi_runtime.structured_log` and writes a
1-page rollup to `kavi-runtime/runtime_metrics/error_budget_<UTC-date>.md`.
Sends the rollup body via `graph.send_mail` to Megha at 7am.

What's in the rollup:
- handler invocation count (email_arrived, imessage_received)
- success rate per handler
- alert count
- P95 latency on email_arrived and imessage_received (computed from
  matched start/done pairs)
- total Anthropic spend (today, from the structured log if usage rows are
  present; falls back to "n/a" if usage isn't logged yet)
- Goodhart watch: if any single error class spiked >3x the 7-day average,
  flag it inline.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from kavi_runtime.structured_log import iter_events

logger = logging.getLogger(__name__)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _events_in_window(log_path: Path, since: datetime, until: datetime) -> list[dict]:
    """Return events within [since, until). Skips rows with unparseable ts."""
    events = []
    for row in iter_events(log_path):
        ts = _parse_ts(row.get("ts", ""))
        if not ts:
            continue
        if since <= ts < until:
            events.append({**row, "_ts": ts})
    return events


def _pair_latencies(events: list[dict], start_event: str, done_event: str,
                    failed_event: str | None = None) -> list[float]:
    """Compute latencies (sec) between consecutive start→(done|failed) pairs.

    Naive pairing: walk events in order; on a start, hold; on the next
    matching done/failed, take the delta. Misses interleaved concurrent
    invocations (which can happen under load), but good enough for a 7am
    rollup signal — bias is consistent day to day."""
    latencies = []
    pending: dict | None = None
    for e in events:
        ev = e.get("event")
        if ev == start_event:
            pending = e
        elif ev in (done_event, failed_event) and pending is not None:
            dt = (e["_ts"] - pending["_ts"]).total_seconds()
            if dt >= 0:
                latencies.append(dt)
            pending = None
    return latencies


def compute_rollup(log_path: Path, *, now: datetime | None = None,
                   window_hours: int = 24) -> dict[str, Any]:
    """Return aggregated stats for the last `window_hours` hours."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    week_since = now - timedelta(days=7)

    today_events = _events_in_window(log_path, since, now)
    week_events = _events_in_window(log_path, week_since, now)

    def counts(events: list[dict]) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for e in events:
            c[e.get("event", "?")] += 1
        return dict(c)

    today_counts = counts(today_events)
    # 7-day daily average (rough; per-day not strictly necessary for Goodhart watch).
    week_counts = counts(week_events)

    email_starts = today_counts.get("email_arrived_start", 0)
    email_done = today_counts.get("email_arrived_done", 0)
    email_failed = today_counts.get("email_arrived_failed", 0)
    imsg_starts = today_counts.get("imessage_received_start", 0)
    imsg_done = today_counts.get("imessage_received_done", 0)
    imsg_failed = today_counts.get("imessage_received_failed", 0)

    email_success_rate = (
        email_done / (email_done + email_failed) if (email_done + email_failed) > 0 else None
    )
    imsg_success_rate = (
        imsg_done / (imsg_done + imsg_failed) if (imsg_done + imsg_failed) > 0 else None
    )

    email_latencies = _pair_latencies(
        today_events, "email_arrived_start", "email_arrived_done", "email_arrived_failed",
    )
    imsg_latencies = _pair_latencies(
        today_events, "imessage_received_start", "imessage_received_done", "imessage_received_failed",
    )

    alerts = [e for e in today_events if e.get("category") == "alert"]
    errors = [e for e in today_events if e.get("category") == "error" or "failed" in (e.get("event") or "")]

    # Goodhart watch: any error event today >3x the 7-day daily average.
    goodhart_flags: list[str] = []
    for ev_name, today_n in today_counts.items():
        if "failed" not in ev_name and ev_name not in {e.get("event") for e in errors}:
            continue
        week_n = week_counts.get(ev_name, 0)
        # 7-day daily average; days observed = 7. Avoid div-by-zero / triggering on
        # first-ever occurrence (require at least 3 over the week).
        daily_avg = week_n / 7.0
        if today_n >= 3 and daily_avg > 0 and today_n > 3 * daily_avg:
            goodhart_flags.append(
                f"{ev_name}: today={today_n} vs 7d-avg={daily_avg:.1f} (>3x)"
            )

    # Anthropic spend today: scan structured log for events that carry token
    # usage. Pricing dedupe (2026-06-10): rates live in kavi_runtime.pricing,
    # not inline. Field names: `_log_call_done` writes the short names
    # `cache_creation` / `cache_read`; this function previously read ONLY
    # the long `*_input_tokens` names and silently missed cache-write cost
    # (the dominant component) on every live row. Both conventions are now
    # accepted via pricing.cost_usd_from_usage.

    def _cost_for_events(events: list[dict]) -> tuple[float, bool]:
        """Sum per-model-priced cost across `events`. Returns (cost, found_any).
        Caller decides whether to surface as None when no usage rows exist."""
        from kavi_runtime.pricing import cost_usd_from_usage

        total = 0.0
        seen = False
        for e in events:
            if e.get("category") != "anthropic":
                continue
            usage = e.get("usage") if isinstance(e.get("usage"), dict) else e
            i = usage.get("input_tokens") or 0
            o = usage.get("output_tokens") or 0
            cw = usage.get("cache_creation") or usage.get("cache_creation_input_tokens") or 0
            cr = usage.get("cache_read") or usage.get("cache_read_input_tokens") or 0
            if any((i, o, cw, cr)):
                seen = True
                total += cost_usd_from_usage(e.get("model"), {
                    "input_tokens": i,
                    "output_tokens": o,
                    "cache_creation": cw,
                    "cache_read": cr,
                })
        return total, seen

    today_cost, today_has_usage = _cost_for_events(today_events)
    week_cost, week_has_usage = _cost_for_events(week_events)
    spend_usd: float | None = today_cost if today_has_usage else None
    week_spend_usd: float | None = week_cost if week_has_usage else None
    # Daily average computed off the 7-day total. Captures rest days more
    # honestly than the prior "days_present" approach — Megha sees one
    # number that scales linearly with monthly burn rate.
    daily_avg_usd: float | None = (week_cost / 7.0) if week_has_usage else None

    # Goodhart watch (item 5, 2026-05-06 batch 6): if 24h spend > $7
    # (≈2x the current daily average ~$3.85), append a flag line. The
    # threshold is intentionally simple — it's a noticeable spike, not a
    # statistical anomaly. Tunable later as the steady-state shifts.
    SPEND_SPIKE_THRESHOLD_USD = 7.0
    if spend_usd is not None and spend_usd > SPEND_SPIKE_THRESHOLD_USD:
        goodhart_flags.append(
            f"anthropic_spend_24h: ${spend_usd:.2f} > ${SPEND_SPIKE_THRESHOLD_USD:.2f} threshold "
            f"(daily avg ${daily_avg_usd:.2f})"
            if daily_avg_usd is not None
            else f"anthropic_spend_24h: ${spend_usd:.2f} > ${SPEND_SPIKE_THRESHOLD_USD:.2f} threshold"
        )

    return {
        "window_start": since.isoformat(),
        "window_end": now.isoformat(),
        "email_starts": email_starts,
        "email_done": email_done,
        "email_failed": email_failed,
        "email_success_rate": email_success_rate,
        "email_p95_latency_sec": _percentile(email_latencies, 95),
        "imessage_starts": imsg_starts,
        "imessage_done": imsg_done,
        "imessage_failed": imsg_failed,
        "imessage_success_rate": imsg_success_rate,
        "imessage_p95_latency_sec": _percentile(imsg_latencies, 95),
        "alerts_count": len(alerts),
        "errors_count": len(errors),
        "anthropic_spend_usd": spend_usd,
        "anthropic_spend_7day_usd": week_spend_usd,
        "anthropic_spend_daily_avg_usd": daily_avg_usd,
        "goodhart_flags": goodhart_flags,
    }


def render_markdown(rollup: dict[str, Any]) -> str:
    """1-page markdown rollup. PM-voiced, what-Megha-notices first."""
    def pct(v: float | None) -> str:
        return f"{v * 100:.1f}%" if v is not None else "n/a"

    def usd(v: float | None) -> str:
        return f"${v:.2f}" if v is not None else "n/a"

    # Cost summary line (added 2026-05-06 batch 6, item 1b). Always one
    # line in the rollup so Megha can read the burn rate at a glance
    # without opening the dashboard.
    cost_line = (
        f"**Anthropic spend last 24h:** {usd(rollup.get('anthropic_spend_usd'))}. "
        f"**7-day total:** {usd(rollup.get('anthropic_spend_7day_usd'))}. "
        f"**Daily average:** {usd(rollup.get('anthropic_spend_daily_avg_usd'))}."
    )

    goodhart = rollup.get("goodhart_flags") or []
    goodhart_line = (
        "\n".join(f"- WATCH: {f}" for f in goodhart) if goodhart else "- none"
    )

    return (
        f"# Kavi runtime — error budget — last 24h\n"
        f"\n"
        f"Window: {rollup['window_start']} → {rollup['window_end']} (UTC)\n"
        f"\n"
        f"## What Megha would notice\n"
        f"- Email handler success rate: {pct(rollup['email_success_rate'])} "
        f"({rollup['email_done']} done / {rollup['email_failed']} failed)\n"
        f"- iMessage handler success rate: {pct(rollup['imessage_success_rate'])} "
        f"({rollup['imessage_done']} done / {rollup['imessage_failed']} failed)\n"
        f"- Alerts fired: {rollup['alerts_count']}\n"
        f"- {cost_line}\n"
        f"\n"
        f"## Latency (P95)\n"
        f"- email_arrived: {rollup['email_p95_latency_sec']:.1f}s\n"
        f"- imessage_received: {rollup['imessage_p95_latency_sec']:.1f}s\n"
        f"\n"
        f"## Goodhart watch (>3x 7-day average; 24h spend > $7)\n"
        f"{goodhart_line}\n"
        f"\n"
        f"## Volume\n"
        f"- email_arrived starts: {rollup['email_starts']}\n"
        f"- imessage_received starts: {rollup['imessage_starts']}\n"
        f"- error category events: {rollup['errors_count']}\n"
    )


def daily_error_budget_job(config: dict, *, now: datetime | None = None) -> None:
    """7am scheduler hook. Compute, write to disk, send to Megha. Never raises."""
    try:
        log_path = Path(config.get("paths", {}).get("structured_log") or
                        (Path.home() / "Library" / "Logs" / "kavi-runtime.json.log"))
        rollup = compute_rollup(log_path, now=now)
        body_md = render_markdown(rollup)

        # Config-driven data dir (2026-09-23), same rule as every runtime
        # write: never derive a data path from this file's location.
        _paths = config.get("paths", {})
        out_dir = Path(
            _paths.get("metrics_dir")
            or (Path(_paths["runtime_events_jsonl"]).parent if _paths.get("runtime_events_jsonl")
                else Path.home() / "HomeOS" / "metrics")
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        date_str = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        out_path = out_dir / f"error_budget_{date_str}.md"
        out_path.write_text(body_md)
        logger.info("error_budget rollup written to %s", out_path)

        # Send via Graph mail. Lazy import to avoid pulling httpx into tests.
        try:
            from kavi_runtime.handlers import _get_clients  # noqa: WPS433
            graph, _, _ = _get_clients(config)
            from kavi_runtime import household as _household
            graph.send_mail(
                to=_household.primary_email("megha"),
                subject=f"Kavi runtime — error budget — {date_str}",
                body=body_md,
                bypass_scanner=True,  # runtime-to-self alert (see send_mail docstring)
            )
        except Exception:
            logger.exception("error_budget: send_mail failed (rollup still on disk)")
    except Exception:
        logger.exception("error_budget rollup failed")
