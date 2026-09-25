"""One-time stop-the-bleed for the 2026-06-27 stuck-pause outage.

A `user_requested_quiet` pause with no `paused_until` gated the email->task
path for 17 days; ~912 notifications piled up in `paused_email_queue`, zero
tasks created. Megha's call (2026-07-14): REPLAY THE LAST 7 DAYS, drop older.

What this does (only with --execute):
  1. Load the paused queue, collapse to unique message_id.
  2. Fetch each unique message and read receivedDateTime; KEEP those received
     within the last N days (default 7), DROP older ones.
  3. Rewrite `paused_email_queue` to only the kept (recent) notifications.
  4. Call the normal resume (`_handle_resume`) which dedups + spend-guards +
     drains the kept queue through email_arrived and clears the pause.

Dry-run by default: prints the counts (unique / per-account / within-window /
gone-from-inbox) and makes NO writes, NO Graph task creation, NO sends. This is
throwaway remediation code, NOT wired into the scheduler — the permanent fix
(time-boxed quiet + auto-resume + watchdog) prevents recurrence.

Run ON KAVI:
    cd ~/kavi-runtime && .venv/bin/python scripts/remediate_stuck_pause.py            # dry-run
    cd ~/kavi-runtime && .venv/bin/python scripts/remediate_stuck_pause.py --execute  # do it
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv(Path.home() / ".config" / "kavi" / ".env")

from kavi_runtime.graph_client import GraphClient  # noqa: E402
from kavi_runtime.state_per_concept import load_pause_state, save_pause_state  # noqa: E402

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
logger = logging.getLogger("remediate_stuck_pause")


def load_config(path: Path = _CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _message_id(n: Any) -> str:
    if not isinstance(n, dict):
        return ""
    rd = n.get("resourceData") or {}
    return rd.get("id") or n.get("resource", "").split("/")[-1] or ""


def _parse_received(dt_str: str) -> datetime | None:
    s = (dt_str or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7, help="replay window in days (default 7)")
    ap.add_argument("--execute", action="store_true",
                    help="actually filter the queue + resume (default: dry-run report only)")
    args = ap.parse_args(argv)

    config = load_config()
    state_path = Path(config["paths"]["imessage_state"])
    ps = load_pause_state(state_path)

    if not ps.get("auto_runs_paused"):
        print("Not paused. Nothing to remediate.")
        return 0
    reason = ps.get("paused_reason")
    queue = ps.get("paused_email_queue", [])
    print(f"Paused reason: {reason}; queued notifications: {len(queue)}")
    if reason != "user_requested_quiet":
        print(f"Refusing: this remediation only handles user_requested_quiet, not {reason!r}.")
        return 2

    # Dedup by message_id (keep first occurrence).
    seen: set[str] = set()
    unique: list[Any] = []
    for n in queue:
        mid = _message_id(n)
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        unique.append(n)
    print(f"Unique message_ids: {len(unique)} ({len(queue) - len(unique)} duplicate re-fires collapsed)")

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    graph = GraphClient(config)

    kept: list[Any] = []
    dropped_old = 0
    gone = 0
    per_account_kept: dict[str, int] = {}
    for i, n in enumerate(unique, 1):
        mid = _message_id(n)
        account = n.get("_source_account") if isinstance(n, dict) else None
        try:
            msg = graph.fetch_message(mid, account=account)
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch failed id=%s account=%s: %s", (mid or "")[:16], account, e)
            msg = None
        if msg is None:
            gone += 1
            continue
        received = _parse_received(msg.get("receivedDateTime", ""))
        if received is None or received < cutoff:
            dropped_old += 1
            continue
        kept.append(n)
        per_account_kept[account or "default"] = per_account_kept.get(account or "default", 0) + 1
        if i % 50 == 0:
            print(f"  ...scanned {i}/{len(unique)} (kept {len(kept)} so far)")

    print("\n--- Plan ---")
    print(f"Replay window: last {args.days} days (received >= {cutoff.isoformat()})")
    print(f"KEEP (become tasks): {len(kept)}  by account: {per_account_kept}")
    print(f"DROP (older than window): {dropped_old}")
    print(f"GONE (no longer in inbox): {gone}")

    if not args.execute:
        print("\nDRY-RUN — no changes made. Re-run with --execute to filter + resume.")
        return 0

    # Rewrite the queue to only the kept (recent) notifications, then resume.
    ps2 = load_pause_state(state_path)
    ps2["paused_email_queue"] = kept
    save_pause_state(state_path, ps2)
    print(f"\nRewrote paused_email_queue to {len(kept)} recent notifications.")

    from kavi_runtime.runtime.pause import _handle_resume
    print("Resuming (dedup + spend-guarded drain + clear)...")
    result = _handle_resume(state_path, config, {"reason": f"one-time remediation: replay last {args.days} days"})
    print(f"Resume result: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
