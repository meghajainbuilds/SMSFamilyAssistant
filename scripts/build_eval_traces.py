#!/usr/bin/env python3
"""Build weekly trace inputs for the HomeOS eval viewer.

Pulls a date-windowed slice of Kavi's runtime judgment feed and writes a
viewer-ready JSONL into the capability's `evals/<slug>/traces/` folder.

Surfaces:
  - inbox : pulls /Users/kavi/HomeOS/evals/inbox-to-task/eval-inbox-judgments.jsonl
            over SSH (raw cat, no /evals/... HTTP cap) and produces
            evals/inbox-to-task/traces/eval-inbox-week<N>.jsonl with one
            row per email decision plus a `readable_text` blob the viewer
            renders directly.

  - persona : stub. Persona week-1 was built manually; this surface is a
              placeholder so the CLI shape is consistent.

Why SSH (not HTTP): the runtime's /evals/inbox-to-task/recent endpoint caps
at 500 rows per day, but the 2026-05-27 backfill produced 1334 rows in a
single day. Raw read sidesteps that cap.

Atomic writes: temp filename includes both PID and a UUID4 segment so two
concurrent invocations (or a stuck retry colliding with a fresh run) cannot
corrupt each other's output. This is the same failure mode that caused the
2026-05-06 Kavi-silent incident; the lesson generalizes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kavi-runtime" / "scripts"))
import deploy_env  # noqa: E402  (KAVI_HOST from env or kavi-runtime/.deploy.env)

KAVI_HOST = deploy_env.get("KAVI_HOST") or ""
INBOX_REMOTE_PATH = "/Users/kavi/HomeOS/evals/inbox-to-task/eval-inbox-judgments.jsonl"

REPO_ROOT = Path(__file__).resolve().parent.parent

# Raw runtime decision values -> normalized viewer decision values.
# (skipped, created) are obvious. `dedup_hit` and `updated` both mean the
# email landed on an existing package/task rather than creating a new one,
# so they collapse into `package_update` for labeling purposes. Raw decision
# is still preserved in the row as `raw_decision` for traceability.
DECISION_NORMALIZE = {
    "skipped": "skip",
    "created": "create",
    "dedup_hit": "dedup_hit",
    "updated": "updated",
}


# ---------- time helpers ----------

def parse_iso_utc(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Accept both 'Z' suffix and '+00:00'.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None


def format_pt(utc_iso: str) -> str:
    dt = parse_iso_utc(utc_iso)
    if not dt:
        return utc_iso or ""
    local = dt.astimezone(PT)
    # e.g. "Tue May 27, 9:00 PM PT"
    return local.strftime("%a %b %-d, %-I:%M %p PT")


# ---------- fetch + filter ----------

def fetch_inbox_rows(remote_path: str = INBOX_REMOTE_PATH) -> list[dict]:
    """Stream the runtime judgments JSONL from Kavi over SSH and parse it.

    Raises CalledProcessError if SSH fails; that's fatal and the caller
    surfaces it. Empty lines are skipped silently.
    """
    cmd = ["ssh", KAVI_HOST or deploy_env.require("KAVI_HOST"), f"cat {remote_path}"]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    rows: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # One malformed row shouldn't kill the build; log and continue.
            print(f"warn: skipped unparseable row ({len(line)} chars)", file=sys.stderr)
    return rows


def in_window(row: dict, start_utc: datetime, end_utc: datetime) -> bool:
    ts = parse_iso_utc(row.get("ts", ""))
    if not ts:
        return False
    return start_utc <= ts <= end_utc


def is_backfill(row: dict) -> bool:
    return row.get("backfill_source") is not None


# ---------- shape ----------

def render_readable_text(row: dict, normalized_decision: str, ts_pt: str) -> str:
    """Multi-line flat rendering of one decision for the viewer card."""
    owner = row.get("task_owner") or "—"
    confidence = row.get("confidence") or "—"
    task_title = row.get("task_title")
    if not task_title:
        # If this was a skip and there's a proposed_title in extras, surface
        # that so Megha can see what Kavi *would* have created. Keeps the
        # "what was almost made" signal visible while labeling.
        extras = row.get("extras") or {}
        proposed = extras.get("proposed_title")
        if proposed:
            task_title = f"(skipped — proposed was: {proposed})"
        else:
            task_title = "(none — skipped)"

    lines = [
        f"[{ts_pt}] {row.get('source_account', '')}",
        f"From: {row.get('sender', '')}",
        f"Subject: {row.get('subject', '')}",
        (
            f"Decision: {normalized_decision}"
            f"  |  Owner: {owner}"
            f"  |  Confidence: {confidence}"
        ),
        f"Task title: {task_title}",
        f"Reason: {row.get('reason') or '(none)'}",
    ]
    if normalized_decision in ("dedup_hit", "updated"):
        tier = row.get("package_match_tier") or "—"
        merge_target = row.get("merge_target_task_id") or "—"
        lines.append(f"[Package match: {tier}, merge → {merge_target}]")
    return "\n".join(lines)


def shape_row(raw: dict) -> dict:
    """Project a runtime judgment row into the viewer-input shape."""
    raw_decision = raw.get("decision") or ""
    normalized = DECISION_NORMALIZE.get(raw_decision, raw_decision or "skip")
    ts = raw.get("ts", "")
    ts_pt = format_pt(ts)
    return {
        "row_id": raw.get("decision_id"),
        "ts": ts,
        "ts_pt": ts_pt,
        "capability": "inbox-to-task",
        "email_id": raw.get("email_id"),
        "source_account": raw.get("source_account"),
        "sender": raw.get("sender"),
        "subject": raw.get("subject"),
        "decision": normalized,
        "raw_decision": raw_decision,
        "reason": raw.get("reason"),
        "owner": raw.get("task_owner"),
        "confidence": raw.get("confidence"),
        "task_title": raw.get("task_title"),
        "package_match_tier": raw.get("package_match_tier"),
        "merge_target_task_id": raw.get("merge_target_task_id"),
        "backfill_source": raw.get("backfill_source"),
        "readable_text": render_readable_text(raw, normalized, ts_pt),
    }


# ---------- atomic write ----------

def atomic_write_jsonl(rows: Iterable[dict], target: Path) -> None:
    """Write JSONL to a unique temp file, then rename into place.

    Unique temp filename (PID + UUID4) is the fix for the 2026-05-06 shared
    .tmp corruption pattern.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    tmp_path = target.parent / tmp_name
    with tmp_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")
    os.replace(tmp_path, target)


# ---------- surfaces ----------

def build_inbox(week: int, days: int, include_backfill: bool) -> tuple[Path, dict]:
    now_utc = datetime.now(timezone.utc)
    # End is now (inclusive); start is `days` days back at the same instant.
    end_utc = now_utc
    start_utc = end_utc - timedelta(days=days)

    raw_rows = fetch_inbox_rows()
    total = len(raw_rows)

    in_range = [r for r in raw_rows if in_window(r, start_utc, end_utc)]
    in_range_count = len(in_range)

    if include_backfill:
        kept = in_range
    else:
        kept = [r for r in in_range if not is_backfill(r)]
    kept_count = len(kept)

    # Sort chronologically so the viewer reads in time order.
    kept.sort(key=lambda r: r.get("ts", ""))
    shaped = [shape_row(r) for r in kept]

    target = REPO_ROOT / "evals" / "inbox-to-task" / "traces" / f"eval-inbox-week{week}.jsonl"
    atomic_write_jsonl(shaped, target)

    summary = {
        "total_rows_on_kavi": total,
        "rows_in_window": in_range_count,
        "rows_after_backfill_filter": kept_count,
        "backfill_excluded": in_range_count - kept_count if not include_backfill else 0,
        "window_start_utc": start_utc.isoformat(),
        "window_end_utc": end_utc.isoformat(),
        "window_days": days,
        "output_path": str(target),
    }
    return target, summary


def build_persona(week: int, days: int) -> None:
    print(
        "persona surface is not implemented in this script.\n"
        "Persona week-1 was built manually; see "
        "evals/kavi-persona/traces/eval-persona-week1.jsonl for the existing input.\n"
        "Add an automated persona builder when the session-grouping logic moves "
        "out of the ad-hoc notebook it currently lives in.",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------- main ----------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build weekly eval-viewer trace inputs.")
    p.add_argument("--surface", required=True, choices=["inbox", "persona"])
    p.add_argument("--week", type=int, required=True,
                   help="Week number (used in the output filename: eval-<scope>-week<N>.jsonl)")
    p.add_argument("--days", type=int, default=7,
                   help="Window size in days, counted back from now (default 7)")
    p.add_argument("--include-backfill", action="store_true",
                   help="By default, rows with backfill_source set are excluded. "
                        "Pass this flag to include them.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.surface == "persona":
        build_persona(args.week, args.days)
        return 0
    # inbox
    target, summary = build_inbox(args.week, args.days, args.include_backfill)
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
