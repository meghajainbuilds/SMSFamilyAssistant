"""One-shot date-range email backfill for the inbox-to-task pipeline.

Why this script exists
======================
The Microsoft Graph subscription for Megha's mailbox expired on 2026-05-14
and was refreshed 12 days later on 2026-05-26. During the gap, no webhook
fired, so the inbox-to-task classifier never ran on those emails. They
still live in the Outlook inbox; this script fetches them by date range
and replays each one through the existing classifier, creating MS To Do
tasks where appropriate.

Safety contract
===============
- iMessage suppression is non-negotiable. The script never instantiates
  a BlueBubblesClient during per-email processing. ONE summary iMessage
  fires at the end, composed deterministically (not LLM-composed).
- Cost cap: a hard USD ceiling (default $10). After every classifier
  call the running spend is recomputed; if the projected total would
  exceed the cap the run aborts gracefully and the summary still sends.
- Dedup: existing semantic dedup against the last N open MS To Do tasks
  protects against creating duplicates of tasks Megha already has. The
  Graph linkedResource externalId lookup catches the exact-message-id
  case for re-runs.
- Crash safety: a checkpoint file is written every 20 emails so a re-run
  resumes from the last processed message_id. Partial JSONL rows on disk
  also count as "already processed" via the existing dedup path.

CLI
===
    .venv/bin/python -m scripts.backfill_email_range \\
        --start 2026-05-14 --end 2026-05-26 \\
        --account megha@example.com \\
        [--dry-run] [--cost-cap 10.0]

`--dry-run` classifies only; no MS To Do writes, no JSONL writes, no
iMessage. Use it first to validate the email count and projected cost
before doing the real run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load env from Kavi's secret store before importing kavi_runtime so the
# Anthropic + Graph credentials are available to constructors.
load_dotenv(Path.home() / ".config" / "kavi" / ".env")

from kavi_runtime.bluebubbles_client import BlueBubblesClient
from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.graph_client import GraphClient
from kavi_runtime.runtime.guardrails import compute_call_cost_usd
from kavi_runtime.handlers import (
    OWNER_ABBREV,
    _eval_inbox_judgments_path,
    _normalize_email,
    _resolve_shared_list_id,
)
from kavi_runtime.state import append_run, utc_now_iso

# Inline config loader (avoids importing kavi_runtime.main which pulls in
# uvicorn / FastAPI at import time — unwanted in a one-shot CLI script).
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path = _CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

logger = logging.getLogger("backfill_email_range")

BACKFILL_TAG = "2026-05-14_to_2026-05-26"
CHECKPOINT_INTERVAL = 20
from kavi_runtime import household as _household  # noqa: E402

SUMMARY_HANDLE = _household.primary_phone("megha")  # Megha's iMessage handle (private config)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill a date-range of Outlook email through the inbox-to-task classifier.",
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD (UTC, inclusive).")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (UTC, inclusive).")
    parser.add_argument("--account", required=True, help="Mailbox owner email, e.g. megha@example.com.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify only. No MS To Do writes, no JSONL appends, no iMessage at the end.",
    )
    parser.add_argument(
        "--cost-cap",
        type=float,
        default=10.0,
        help="Hard USD ceiling for Anthropic spend during the run (default 10.0).",
    )
    parser.add_argument(
        "--checkpoint-path",
        default=None,
        help="Override the default checkpoint path. Default writes alongside the runtime state.",
    )
    return parser.parse_args(argv)


def _date_bounds_utc(start: str, end: str) -> tuple[str, str]:
    """Return ISO-8601 UTC strings for the start-of-start-day and the
    end-of-end-day. Graph's $filter on receivedDateTime is half-open on
    seconds; end-of-day is rendered as 23:59:59 to be inclusive of the
    end calendar date in UTC."""
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc,
    )
    if end_dt < start_dt:
        raise SystemExit(f"--end ({end}) must be on or after --start ({start})")
    return (
        start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def list_messages_in_range(
    graph: GraphClient,
    start_utc: str,
    end_utc: str,
    *,
    account: str,
) -> list[dict[str, Any]]:
    """Thin pass-through to GraphClient.list_messages_in_range. Kept as a
    module-level function so tests can patch it via `patch.object(bf, ...)`
    without needing to instantiate a GraphClient."""
    return graph.list_messages_in_range(start_utc, end_utc, account=account)


def _checkpoint_path(config: dict, override: str | None) -> Path:
    if override:
        return Path(override)
    state_dir = Path(config["paths"]["imessage_state"]).parent
    return state_dir / f"backfill_state_{BACKFILL_TAG}.json"


def _load_checkpoint(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data.get("processed_ids", []))
    except Exception as e:
        logger.warning("checkpoint load failed (starting fresh): %s", e)
        return set()


def _save_checkpoint(path: Path, processed_ids: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "backfill_tag": BACKFILL_TAG,
        "updated_at": utc_now_iso(),
        "processed_ids": sorted(processed_ids),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def _decision_id(email_id: str | None) -> str:
    suffix = (email_id or "noid")[:16]
    return f"d_{utc_now_iso()}_{suffix}"


def _summary_text(processed: int, created: int, skipped: int, runtime_sec: float) -> str:
    """Deterministic summary string. Plain language; no engineering jargon."""
    minutes = max(1, round(runtime_sec / 60))
    return (
        f"Backfill done. {processed} emails processed, {created} tasks created, "
        f"{skipped} skipped. {minutes} minutes runtime. Open MS To Do to review."
    )


def _send_summary_imessage(config: dict, text: str) -> bool:
    """Single deterministic send at the end of the run. Returns True on
    verified delivery; False otherwise. Failure here is non-fatal: the
    JSONL rows already on disk are the source of truth."""
    try:
        bb = BlueBubblesClient(config)
        temp_guid = f"backfill_summary-{uuid.uuid4().hex[:8]}"
        result = bb.send_with_verify(
            text, temp_guid=temp_guid, recipient_handle=SUMMARY_HANDLE,
        )
        verified = bool(result.get("verified"))
        logger.info("summary iMessage send: verified=%s", verified)
        return verified
    except Exception as e:
        logger.warning("summary iMessage send failed: %s", e)
        return False


def _log_judgment_row(
    config: dict,
    *,
    email_payload: dict | None,
    message_id: str | None,
    decision: str,
    reason: str | None,
    confidence: str | None,
    task_id: str | None,
    task_title: str | None,
    task_owner: str | None,
    usage: dict | None,
    source_account: str | None,
    dry_run: bool,
) -> None:
    """Append one row to eval-inbox-judgments.jsonl matching the existing
    schema (see evals/definitions.md). Adds a `backfill_source` marker so
    future readers can identify backfill rows.

    When `dry_run` is True this is a no-op.
    """
    if dry_run:
        return
    sender = None
    subject = None
    if email_payload:
        sender = email_payload.get("from_address") or email_payload.get("from_name")
        subject = (email_payload.get("subject") or "")[:200]

    record = {
        "decision_id": _decision_id(message_id),
        "ts": utc_now_iso(),
        "capability": "inbox-to-task",
        "email_id": message_id,
        "sender": sender,
        "subject": subject,
        "decision": decision,
        "reason": reason,
        "confidence": confidence,
        "task_id": task_id,
        "task_title": task_title,
        "task_owner": task_owner,
        "imessage_sent": False,  # backfill never fires per-task iMessage.
        "latency_first_action_sec": None,
        "usage": usage,
        "package_id": None,
        "package_match_tier": "none",
        "merge_target_task_id": None,
        "merge_reason": None,
        "lifecycle_state": None,
        "task_body_length": None,
        "source_account": source_account,
        "backfill_source": BACKFILL_TAG,
    }
    try:
        append_run(_eval_inbox_judgments_path(config), record)
    except Exception as e:
        logger.warning("judgment row append failed (continuing): %s", e)


def process_one_email(
    msg: dict[str, Any],
    *,
    config: dict,
    graph: GraphClient,
    claude: ClaudeClient,
    account: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Classify one email and (when not dry-run) create the task. Returns
    a small dict describing the outcome plus the usage so the caller can
    update running spend.

    Does NOT instantiate or call BlueBubbles. Does NOT call any quiet-
    hours / digest / pending-questions path. Per the backfill safety
    contract, the only outbound side effects are MS To Do writes plus the
    one summary iMessage at the very end of the run.
    """
    email_payload = _normalize_email(msg)
    email_payload["source_account"] = account
    message_id = msg.get("id") or email_payload.get("id")

    # Run the existing classifier. Same call shape email_arrived makes.
    try:
        result = claude.run_email_to_tasks(email_payload, thread_messages=[])
    except Exception as e:
        logger.exception("classifier call failed for message_id=%s: %s", (message_id or "")[:16], e)
        return {
            "outcome": "error",
            "reason": f"classifier_exception: {e}",
            "usage": None,
            "message_id": message_id,
            "email_payload": email_payload,
        }

    usage = result.get("_usage")

    if result.get("status") != "task":
        reason = result.get("reason", "")[:200]
        _log_judgment_row(
            config,
            email_payload=email_payload,
            message_id=message_id,
            decision="skipped",
            reason=reason,
            confidence=None,
            task_id=None,
            task_title=None,
            task_owner=None,
            usage=usage,
            source_account=account,
            dry_run=dry_run,
        )
        return {
            "outcome": "skipped",
            "reason": reason,
            "usage": usage,
            "message_id": message_id,
            "email_payload": email_payload,
        }

    task = result["task"]
    task["source_email_id"] = message_id
    task["source_subject"] = email_payload.get("subject", "")

    if dry_run:
        return {
            "outcome": "would_create",
            "task": task,
            "usage": usage,
            "message_id": message_id,
            "email_payload": email_payload,
        }

    list_id = _resolve_shared_list_id(graph, account, config)

    # Exact-id dedup via linkedResource externalId. This protects re-runs:
    # an earlier partial run that already created a task for this email_id
    # will hit here and short-circuit cleanly.
    try:
        existing = graph.find_todo_task_by_source_email(list_id, message_id, account=account)
    except Exception as e:
        logger.warning("graph dedup lookup failed (continuing without): %s", e)
        existing = None
    if existing:
        _log_judgment_row(
            config,
            email_payload=email_payload,
            message_id=message_id,
            decision="dedup_hit",
            reason="graph_linked_resource",
            confidence=task.get("confidence"),
            task_id=existing,
            task_title=None,
            task_owner=task.get("owner"),
            usage=usage,
            source_account=account,
            dry_run=False,
        )
        return {
            "outcome": "dedup_hit",
            "task_id": existing,
            "usage": usage,
            "message_id": message_id,
            "email_payload": email_payload,
        }

    # Semantic dedup against open tasks in the shared list. Same call shape
    # email_arrived uses so the LLM matcher sees the same context.
    owner_abbrev = OWNER_ABBREV.get(task.get("owner", ""), "??")
    proposed_title_for_dedup = f"{owner_abbrev} {task.get('title', '')}"
    if task.get("confidence") == "low":
        proposed_title_for_dedup = f"[?] {proposed_title_for_dedup}"
    try:
        open_tasks_raw = graph.list_open_todo_tasks(list_id, top=100, account=account)
        recent_active = [
            {"id": t["id"], "title": t.get("title", "")}
            for t in open_tasks_raw
            if t.get("title")
        ]
    except Exception as e:
        logger.warning("semantic dedup: list_open_todo_tasks failed (continuing): %s", e)
        recent_active = []

    if recent_active:
        try:
            sem_dup = claude.check_semantic_duplicate(proposed_title_for_dedup, recent_active)
        except Exception as e:
            logger.warning("semantic dedup check failed (proceeding with create): %s", e)
            sem_dup = {"is_duplicate": False}
        if sem_dup.get("is_duplicate"):
            matches_id = sem_dup.get("matches_task_id", "")
            _log_judgment_row(
                config,
                email_payload=email_payload,
                message_id=message_id,
                decision="dedup_hit",
                reason=f"semantic: {sem_dup.get('reason', '')[:200]}",
                confidence=task.get("confidence"),
                task_id=matches_id,
                task_title=None,
                task_owner=task.get("owner"),
                usage=usage,
                source_account=account,
                dry_run=False,
            )
            return {
                "outcome": "dedup_hit",
                "task_id": matches_id,
                "usage": usage,
                "message_id": message_id,
                "email_payload": email_payload,
            }

    try:
        task_id = graph.create_todo_task(
            list_id, task, message_id, email_payload.get("subject", ""),
            account=account,
        )
    except Exception as e:
        logger.exception("create_todo_task failed for message_id=%s: %s", (message_id or "")[:16], e)
        _log_judgment_row(
            config,
            email_payload=email_payload,
            message_id=message_id,
            decision="skipped",
            reason=f"create_failed: {e}",
            confidence=task.get("confidence"),
            task_id=None,
            task_title=None,
            task_owner=task.get("owner"),
            usage=usage,
            source_account=account,
            dry_run=False,
        )
        return {
            "outcome": "create_failed",
            "reason": str(e),
            "usage": usage,
            "message_id": message_id,
            "email_payload": email_payload,
        }

    _log_judgment_row(
        config,
        email_payload=email_payload,
        message_id=message_id,
        decision="created",
        reason=None,
        confidence=task.get("confidence"),
        task_id=task_id,
        task_title=task.get("title"),
        task_owner=task.get("owner"),
        usage=usage,
        source_account=account,
        dry_run=False,
    )
    return {
        "outcome": "created",
        "task_id": task_id,
        "usage": usage,
        "message_id": message_id,
        "email_payload": email_payload,
    }


def run_backfill(
    *,
    start: str,
    end: str,
    account: str,
    dry_run: bool,
    cost_cap_usd: float,
    config: dict,
    graph: GraphClient | None = None,
    claude: ClaudeClient | None = None,
    checkpoint_override: str | None = None,
    send_summary: bool = True,
) -> dict[str, Any]:
    """Library entry point. Tests call this with mocked graph + claude and
    `send_summary=False`. The CLI wrapper below builds the real clients.

    Returns a summary dict with counts + spend + summary text + send flag.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if graph is None:
        graph = GraphClient(config)
    if claude is None:
        claude = ClaudeClient(config)

    start_utc, end_utc = _date_bounds_utc(start, end)
    logger.info(
        "backfill range: %s -> %s account=%s dry_run=%s cost_cap=$%.2f",
        start_utc, end_utc, account, dry_run, cost_cap_usd,
    )

    messages = list_messages_in_range(graph, start_utc, end_utc, account=account)
    logger.info("fetched %d messages in window", len(messages))

    checkpoint_path = _checkpoint_path(config, checkpoint_override)
    processed_ids = _load_checkpoint(checkpoint_path)
    if processed_ids:
        logger.info("checkpoint has %d previously processed ids; will skip those", len(processed_ids))

    started = time.monotonic()
    spend_usd = 0.0
    counters = {"processed": 0, "created": 0, "skipped": 0, "dedup_hit": 0, "errors": 0}
    aborted_cost_cap = False
    edge_cases: list[dict[str, Any]] = []

    for i, msg in enumerate(messages, start=1):
        message_id = msg.get("id") or ""
        if message_id in processed_ids:
            logger.debug("skip already-processed message_id=%s", message_id[:16])
            continue
        result = process_one_email(
            msg,
            config=config,
            graph=graph,
            claude=claude,
            account=account,
            dry_run=dry_run,
        )
        outcome = result.get("outcome")
        counters["processed"] += 1
        if outcome == "created" or outcome == "would_create":
            counters["created"] += 1
        elif outcome == "dedup_hit":
            counters["dedup_hit"] += 1
        elif outcome == "skipped":
            counters["skipped"] += 1
        else:
            counters["errors"] += 1

        usage = result.get("usage")
        if usage:
            spend_usd += compute_call_cost_usd(usage)

        # Edge cases worth surfacing in the final report.
        confidence = (result.get("task", {}) or {}).get("confidence") if outcome == "would_create" else None
        if confidence == "low":
            edge_cases.append({
                "message_id": message_id,
                "subject": (result.get("email_payload", {}) or {}).get("subject", "")[:120],
                "confidence": "low",
            })
        if outcome == "dedup_hit":
            edge_cases.append({
                "message_id": message_id,
                "subject": (result.get("email_payload", {}) or {}).get("subject", "")[:120],
                "outcome": "dedup_hit",
                "matched_task_id": result.get("task_id"),
            })

        processed_ids.add(message_id)
        if counters["processed"] % CHECKPOINT_INTERVAL == 0 and not dry_run:
            _save_checkpoint(checkpoint_path, processed_ids)
            logger.info(
                "checkpoint: %d processed, spend=$%.4f", counters["processed"], spend_usd,
            )

        if spend_usd >= cost_cap_usd:
            aborted_cost_cap = True
            logger.warning(
                "cost cap hit: spend=$%.4f >= cap=$%.2f after %d emails; aborting",
                spend_usd, cost_cap_usd, counters["processed"],
            )
            break

    if not dry_run:
        _save_checkpoint(checkpoint_path, processed_ids)

    runtime_sec = time.monotonic() - started
    summary_text = _summary_text(
        counters["processed"], counters["created"], counters["skipped"] + counters["dedup_hit"], runtime_sec,
    )
    if aborted_cost_cap:
        summary_text = (
            f"Backfill stopped at cost cap. {counters['processed']} emails processed, "
            f"{counters['created']} tasks created, {counters['skipped'] + counters['dedup_hit']} skipped. "
            f"{max(1, round(runtime_sec / 60))} minutes runtime. Open MS To Do to review."
        )

    summary_sent = False
    if send_summary and not dry_run:
        summary_sent = _send_summary_imessage(config, summary_text)
    elif send_summary and dry_run:
        logger.info("dry-run: skipping summary iMessage send")

    return {
        "processed": counters["processed"],
        "created": counters["created"],
        "skipped": counters["skipped"],
        "dedup_hit": counters["dedup_hit"],
        "errors": counters["errors"],
        "spend_usd": round(spend_usd, 4),
        "aborted_cost_cap": aborted_cost_cap,
        "runtime_sec": round(runtime_sec, 1),
        "summary_text": summary_text,
        "summary_sent": summary_sent,
        "edge_cases": edge_cases,
        "messages_fetched": len(messages),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    config = load_config()
    result = run_backfill(
        start=args.start,
        end=args.end,
        account=args.account,
        dry_run=args.dry_run,
        cost_cap_usd=args.cost_cap,
        config=config,
        checkpoint_override=args.checkpoint_path,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
