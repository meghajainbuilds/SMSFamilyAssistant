#!/usr/bin/env python3
"""Backfill past-week iMessage exchanges into a session-per-row JSONL for open coding.

Fetches eval-persona-inbound.jsonl + eval-persona-outbound-judgments.jsonl from
the running kavi-runtime on Kavi's Mac via HTTP (Tailscale), merges ALL events
(inbound + outbound, regardless of who initiated) into a single chronological
timeline, and groups them into sessions by time gap.

Each JSONL row = ONE SESSION. Within a session, messages[] is the chronological
list of inbound + outbound events. Each message carries its own metadata.

This replaces the prior "row = one Megha-initiated turn" schema, which dropped
Kavi-initiated messages (qa_question, periodic_summary that Megha replied to,
alert_fallback, task_notification, coordination_addressee_reach, etc.) and so
lost the upstream end of multi-turn sessions. Schema v2: chronological message
timeline, no inbound<->outbound binding at the row level.

Sessionization:
- All events sorted by ts.
- New session starts when gap between consecutive events exceeds
  --session-gap-min minutes (default 30).
- session_id = s_<first_event_ts>_<8hex>.

Backfill limitations (documented inline in the output rows):
- context_at_decision per message: not captured in past-week JSONLs. Null.
- system_prompt_hash per message: not captured. Null.
- Upstream LLM calls (classifier, matcher) not reconstructible from past-week
  data. Only the composing call surfaces in llm_calls[]. Phase B trace_log.py
  captures the full cascade going forward.

Usage:
  python backfill_imessage_exchanges.py \\
    --since 2026-05-05 --until 2026-05-12 \\
    --session-gap-min 30 \\
    --out evals/traces/exchanges-backfill-week1.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_env  # noqa: E402  (KAVI_ADDR from env or kavi-runtime/.deploy.env)

DEFAULT_KAVI_HOST = f"{deploy_env.kavi_addr()}:8080"


def fetch_endpoint(host: str, path: str, date: str, limit: int = 500) -> list[dict]:
    """GET http://<host><path>?date=YYYY-MM-DD&limit=N. Return rows[]."""
    url = f"http://{host}{path}?date={date}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("rows", [])
    except urllib.error.URLError as e:
        print(f"WARN: fetch failed for {url}: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"WARN: unexpected error for {url}: {e}", file=sys.stderr)
        return []


def daterange(since: str, until: str) -> list[str]:
    start = datetime.strptime(since, "%Y-%m-%d").date()
    end = datetime.strptime(until, "%Y-%m-%d").date()
    if end < start:
        raise SystemExit(f"--until ({until}) must be >= --since ({since})")
    out = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def fetch_all(host: str, dates: list[str]) -> tuple[list[dict], list[dict]]:
    """Pull all inbound + outbound rows across the date range. Dedup by id."""
    inbound_seen: set[str] = set()
    outbound_seen: set[str] = set()
    inbound: list[dict] = []
    outbound: list[dict] = []
    for d in dates:
        for r in fetch_endpoint(host, "/evals/kavi-persona/inbound/recent", d, limit=500):
            iid = r.get("inbound_id")
            if iid and iid not in inbound_seen:
                inbound_seen.add(iid)
                inbound.append(r)
        for r in fetch_endpoint(host, "/evals/kavi-persona/recent", d, limit=500):
            did = r.get("decision_id")
            if did and did not in outbound_seen:
                outbound_seen.add(did)
                outbound.append(r)
    return inbound, outbound


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def build_event(row: dict, kind: str) -> dict:
    """Normalize an inbound or outbound row into a uniform timeline event."""
    if kind == "inbound":
        return {
            "kind": "inbound",
            "ts": row.get("ts"),
            "inbound_id": row.get("inbound_id"),
            "text": row.get("text"),
            "sender": row.get("sender"),
            "source": row.get("source", "imessage"),
            "char_count": row.get("char_count"),
            "_raw": row,
        }
    else:
        # Surface actions_executed as inline tool calls per Kavi message.
        ctx = row.get("context") or {}
        actions = ctx.get("actions_executed") or []
        tool_calls = []
        for a in actions:
            tool_calls.append({
                "tool": f"graph.{a.get('action_type', 'unknown')}",
                "args": {
                    k: v for k, v in a.items()
                    if k in ("task_id", "target_title", "owner_prefix", "source_imessage_id")
                },
                "result": {
                    "ok": a.get("result") == "success",
                    "result_raw": a.get("result"),
                    "dedup_hit": a.get("dedup_hit"),
                },
            })
        usage = row.get("usage") or {}
        llm_calls = [{
            "call_type": f"compose_{row.get('kind', 'unknown')}",
            "model": None,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "output_text": row.get("text"),
            "_note": "backfill: input_text/model not captured; Phase B trace_log.py captures full cascade",
        }] if usage else []
        return {
            "kind": "outbound",
            "ts": row.get("ts"),
            "decision_id": row.get("decision_id"),
            "outbound_kind": row.get("kind"),
            "text": row.get("text"),
            "char_count": row.get("char_count"),
            "structural_checks": row.get("structural_checks"),
            "structural_pass": row.get("structural_pass"),
            "fallback_used": row.get("fallback_used"),
            "verified": row.get("verified"),
            "triggered_by": row.get("triggered_by"),
            "context": row.get("context") or {},
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
            "_raw": row,
        }


def sessionize(events: list[dict], gap_min: int) -> list[list[dict]]:
    """Walk events chronologically; start a new session when gap > gap_min."""
    if not events:
        return []
    events.sort(key=lambda e: e["ts"] or "")
    threshold = timedelta(minutes=gap_min)
    sessions: list[list[dict]] = []
    current: list[dict] = [events[0]]
    for prev, ev in zip(events, events[1:]):
        gap = parse_ts(ev["ts"]) - parse_ts(prev["ts"])
        if gap > threshold:
            sessions.append(current)
            current = [ev]
        else:
            current.append(ev)
    if current:
        sessions.append(current)
    return sessions


# Outbound kinds whose semantics imply "responding to something Megha said."
# If a session contains one of these AND has no inbound rows, the inbound was
# likely lost (inbound_log.py was added 2026-05-04 evening; outbound from before
# the logger went live exists without a paired inbound). Surface as a warning
# in the viewer so Megha knows context may be missing.
RESPONSE_KINDS = frozenset({
    "conversational",
    "qa_ack",
    "correction_ack",
    "correction_ack_actions",
    "post_action_reply",
    "post_action_reply_actions",
    "action_clarifying",
    "ack_short",
    "self_check_ack",
    "coordination_ack",
})


def build_session_row(session_events: list[dict]) -> dict:
    """One JSONL row per session. messages[] is the chronological event list."""
    first = session_events[0]
    last = session_events[-1]
    first_ts = first["ts"]
    session_id = f"s_{first_ts}_{uuid.uuid4().hex[:8]}"

    # Distinct participants (sender of any inbound, plus "kavi" if any outbound exists).
    participants = set()
    has_kavi = False
    has_inbound = False
    response_kinds_present: set[str] = set()
    for ev in session_events:
        if ev["kind"] == "inbound" and ev.get("sender"):
            participants.add(ev["sender"])
            has_inbound = True
        if ev["kind"] == "outbound":
            has_kavi = True
            ok = ev.get("outbound_kind")
            if ok in RESPONSE_KINDS:
                response_kinds_present.add(ok)
    if has_kavi:
        participants.add("kavi")

    # Data-gap heuristic: this session has Kavi response-shaped messages but
    # no inbound rows to pair with them. Likely an inbound_log.py gap.
    data_gap_suspected = (not has_inbound) and bool(response_kinds_present)

    # Initiator: who sent the first message.
    initiator = "kavi" if first["kind"] == "outbound" else (first.get("sender") or "unknown")

    # Decision summary across the session — one entry per outbound.
    decision_parts = []
    for i, ev in enumerate(session_events, start=1):
        if ev["kind"] == "outbound":
            flags = []
            if ev.get("structural_pass") is False: flags.append("structural_FAIL")
            if ev.get("verified") is False: flags.append("not_verified")
            if ev.get("fallback_used"): flags.append("fallback_used")
            if not ev.get("triggered_by"): flags.append("kavi_initiated")
            tag = f" [{','.join(flags)}]" if flags else ""
            decision_parts.append(f"#{i} {ev.get('outbound_kind', '?')}{tag}")
    decision_summary = " | ".join(decision_parts) if decision_parts else "(no outbound in session)"

    # Drop the _raw keys to keep the persisted row clean; raw payloads available
    # in eval-persona-{inbound,outbound-judgments}.jsonl on Kavi if needed.
    persisted_messages = [
        {k: v for k, v in ev.items() if k != "_raw"}
        for ev in session_events
    ]

    return {
        "session_id": session_id,
        "first_ts": first_ts,
        "last_ts": last["ts"],
        "channel": "imessage",
        "capability": "kavi-persona",
        "participants": sorted(participants),
        "initiator": initiator,
        "session_size": len(session_events),
        "messages": persisted_messages,
        "decision_summary": decision_summary,
        "data_gap_suspected": data_gap_suspected,
        "data_gap_reason": (
            f"session has Kavi response-shaped messages ({', '.join(sorted(response_kinds_present))}) "
            f"but no inbound rows. inbound_log.py was added 2026-05-04 evening; "
            f"inbound from before deploy may be missing."
        ) if data_gap_suspected else None,
        "_backfill_meta": {
            "source": "backfill_imessage_exchanges.py",
            "schema_version": 2,
            "note": "Schema v2: row = session with messages[] timeline. context_at_decision + system_prompt_hash + upstream LLM calls not available pre-Phase-B.",
        },
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--until", required=True, help="YYYY-MM-DD (inclusive)")
    p.add_argument("--out", required=True, help="Output JSONL path")
    p.add_argument("--kavi-host", default=DEFAULT_KAVI_HOST,
                   help=f"host:port for kavi-runtime (default: {DEFAULT_KAVI_HOST})")
    p.add_argument("--session-gap-min", type=int, default=30,
                   help="New session when gap between consecutive events exceeds N minutes (default: 30)")
    args = p.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dates = daterange(args.since, args.until)
    print(f"Fetching {len(dates)} days: {dates[0]} -> {dates[-1]}", file=sys.stderr)
    deploy_env.check_host(args.kavi_host)
    inbound_rows, outbound_rows = fetch_all(args.kavi_host, dates)
    print(f"  inbound: {len(inbound_rows)} rows", file=sys.stderr)
    print(f"  outbound: {len(outbound_rows)} rows", file=sys.stderr)

    # Build flat timeline of all events.
    events = [build_event(r, "inbound") for r in inbound_rows if r.get("ts")]
    events += [build_event(r, "outbound") for r in outbound_rows if r.get("ts")]
    print(f"  total events on timeline: {len(events)}", file=sys.stderr)

    sessions = sessionize(events, args.session_gap_min)
    print(f"  -> {len(sessions)} sessions (gap > {args.session_gap_min} min)", file=sys.stderr)

    rows = [build_session_row(s) for s in sessions]

    # Sort sessions chronologically by first_ts for label-in-order.
    rows.sort(key=lambda r: r["first_ts"] or "")

    with out_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # Stats for sanity.
    sizes = [r["session_size"] for r in rows]
    kavi_init = sum(1 for r in rows if r["initiator"] == "kavi")
    megha_init = sum(1 for r in rows if r["initiator"] != "kavi")
    multi = sum(1 for r in rows if r["session_size"] > 1)
    one_msg = sum(1 for r in rows if r["session_size"] == 1)
    data_gap = sum(1 for r in rows if r.get("data_gap_suspected"))
    max_size = max(sizes) if sizes else 0

    print(f"\nWrote {len(rows)} sessions to {out_path}", file=sys.stderr)
    print(f"  initiated by Megha: {megha_init}", file=sys.stderr)
    print(f"  initiated by Kavi: {kavi_init}", file=sys.stderr)
    print(f"  multi-message sessions: {multi}", file=sys.stderr)
    print(f"  single-message sessions: {one_msg}", file=sys.stderr)
    print(f"  data-gap suspect sessions: {data_gap}", file=sys.stderr)
    print(f"  largest session: {max_size} messages", file=sys.stderr)
    if len(rows) < 15:
        print(f"\nWARN: only {len(rows)} sessions. Expected >=15 for Week 1 labeling.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
