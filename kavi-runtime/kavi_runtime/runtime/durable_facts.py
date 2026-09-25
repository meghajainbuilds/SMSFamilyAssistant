"""Cross-day durable facts — append-only fact store for Kavi.

Motivating failure (handoff-2026-05-05 Row #2/#3/#5): Kavi promised "wait until
done" then said "On it" again with no memory of the promise. The runtime had no
durable place to record cross-conversation commitments, so each new inbound
turn started from a blank slate.

This module is the v0 storage layer (G-C3 foundation). Cap 2 (`kavi-coordinates`)
will wire reads of `read_active_facts` into the reply-context builder so the
composer sees prior commitments before drafting. THIS MODULE DOES NOT WIRE
INTO THE RUNTIME; it is storage + helpers only.

Schema (one JSON object per line):

  {
    "fact_id": "f_<ISO_ts>_<uuid8>",
    "ts": "<ISO 8601 UTC>",
    "fact_text": "<plain English fact>",
    "scope": "megha" | "max" | "household" | "kavi",
    "source_decision_id": "<id of triggering decision> | null",
    "expires_at": "<ISO 8601 UTC> | null",
    "status": "active" | "superseded" | "expired"
  }

Append-only invariants:
- `record_fact` writes one new "active" row.
- `supersede_fact(fact_id)` writes a NEW "superseded" row referencing the prior
  fact (`source_decision_id` carries the prior fact_id). The prior row is left
  unchanged; readers compute "is this still active" by scanning forward.
- `expire_facts()` writes NEW "expired" rows for any active fact whose
  `expires_at` is in the past.

Read semantics: `read_active_facts` returns the latest row per `fact_id`,
filtered to rows whose effective status is "active." A fact_id whose latest
row is `superseded` or `expired` is excluded.

Concurrency: file-level advisory lock via `fcntl.flock` (POSIX). The lock is
acquired in EXCLUSIVE mode for writes and SHARED mode for reads, then released
on close. Existing append-only files in the runtime (corrections.jsonl,
eval-*-judgments.jsonl, runtime-events.jsonl) rely on POSIX append semantics
without an explicit lock; this module adopts flock because durable facts are
the single source of cross-conversation truth and a torn write would erase a
commitment Kavi promised to keep.

Storage path: configurable via `config.yaml` -> `paths.learned_facts`. Default
fallback: `/Users/kavi/HomeOS/learned_facts.jsonl` when the key is missing.
"""

from __future__ import annotations

import fcntl
import json
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.state_io import atomic_write_bytes

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path("/Users/kavi/HomeOS/learned_facts.jsonl")
_DEFAULT_PENDING_PATH = Path("/Users/kavi/HomeOS/pending_facts.jsonl")

VALID_SCOPES = {"megha", "max", "household", "kavi"}
VALID_STATUSES = {"active", "superseded", "expired"}


# ---- path resolution -------------------------------------------------------


def _facts_path(config: dict | None) -> Path:
    """Resolve the durable-facts JSONL path. Falls back to the default if
    `config` is None or `paths.learned_facts` is missing."""
    if config is None:
        return _DEFAULT_PATH
    paths = config.get("paths") or {}
    raw = paths.get("learned_facts")
    if not raw:
        return _DEFAULT_PATH
    return Path(raw)


def _pending_facts_path(config: dict | None) -> Path:
    """Resolve the pending-facts JSONL path. Pending facts are facts derived
    from inbound (untrusted) content that need explicit Megha confirmation
    before persisting to the durable store. v0 logs them here without
    persisting; Megha approves manually via a tomorrow-task."""
    if config is None:
        return _DEFAULT_PENDING_PATH
    paths = config.get("paths") or {}
    raw = paths.get("pending_facts")
    if raw:
        return Path(raw)
    # Fall back to a sibling of learned_facts so both files live in the same
    # directory without a separate config key.
    facts = _facts_path(config)
    return facts.parent / "pending_facts.jsonl"


# ---- timestamp + id helpers ------------------------------------------------


def _utc_now_iso() -> str:
    """ISO 8601 UTC string, second resolution. Matches the format used in
    inbound_log / outbound_log so fact rows interleave readably with eval rows
    when audited together."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_fact_id() -> str:
    return f"f_{_utc_now_iso()}_{uuid.uuid4().hex[:8]}"


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC datetime. Accepts both
    'Z' and '+00:00' suffix forms."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---- low-level locked io ---------------------------------------------------


def _append_locked(path: Path, record: dict[str, Any]) -> None:
    """Append a single JSONL row under an exclusive flock. Creates the parent
    directory if missing. The lock is held for the duration of the write and
    released by the `with open(...)` context exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_all_locked(path: Path) -> list[dict[str, Any]]:
    """Read every JSONL row under a shared flock. Returns [] when the file
    does not exist. Malformed lines are skipped with a warning so a single
    corrupt write cannot poison the entire fact store."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning(
                        "durable_facts: skipping malformed line %d in %s: %s",
                        lineno,
                        path,
                        e,
                    )
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return rows


# ---- public api ------------------------------------------------------------


def record_fact(
    fact_text: str,
    scope: str,
    source_decision_id: str | None = None,
    expires_at: str | None = None,
    *,
    config: dict | None = None,
    originated_from_inbound_content: bool = False,
    inbound_source: str | None = None,
) -> str | None:
    """Append one active fact row. Returns the new `fact_id`, or None when
    the durable-facts write filter blocks the write OR the fact originated
    from inbound (untrusted) content and is queued for confirmation.

    Args:
      fact_text: plain-English fact (e.g., "Kavi committed to only message
        Megha when a task is actually marked done").
      scope: one of {"megha", "max", "household", "kavi"}.
      source_decision_id: id of the triggering decision (e.g., an outbound
        decision_id from eval-persona-outbound-judgments.jsonl) or None for
        unsourced facts (e.g., explicit user assertion).
      expires_at: ISO 8601 UTC timestamp at which this fact becomes stale, or
        None for indefinite facts.
      config: runtime config dict (optional). When None, the default storage
        path is used.
      originated_from_inbound_content: True when the fact text was derived
        from email body or iMessage text (untrusted content). When True, the
        fact is NOT persisted to learned_facts.jsonl; instead a row is
        written to pending_facts.jsonl with status="pending_confirmation"
        and Megha confirms manually. v0 has no auto-reply path.
      inbound_source: short label describing where the inbound content came
        from (e.g., "email msg-abc123", "imessage from Max"). Recorded on
        the pending row so Megha can identify the source when confirming.

    Raises:
      ValueError: when `scope` is not in VALID_SCOPES, or `fact_text` is empty
        after `.strip()`. Both are caller bugs we want loud, not silent.

    Returns:
      str (fact_id) on a successful write.
      None when the outbound-content scanner blocked the write OR the
      fact originated from inbound content and is now pending confirmation.
      The caller can distinguish via the side effect (block goes to
      outbound_blocked.jsonl; pending goes to pending_facts.jsonl).

    Defense in depth:
    - 2026-05-05: regex scanner gate before disk I/O.
    - 2026-05-06: confirmation gate before disk I/O when content is
      inbound-sourced. Closes the prompt-injection-poisons-memory vector
      identified in the audit. v0 simplification: no auto-reply path —
      pending facts log a TODO marker and Megha approves via tomorrow-task.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(
            f"invalid scope {scope!r}; expected one of {sorted(VALID_SCOPES)}"
        )
    text = fact_text.strip()
    if not text:
        raise ValueError("fact_text must be non-empty after strip()")

    # Outbound-content scanner gate. Imported lazily to avoid creating an
    # import cycle with outbound_scanner (which today does not import
    # durable_facts, but the lazy form keeps this module decoupled if that
    # changes). On a sensitive-pattern hit, the scanner logs the block
    # and we return None without writing.
    from kavi_runtime.runtime import outbound_scanner
    allowed, block_reason = outbound_scanner.gate_durable_fact_write(
        config=config, fact_text=text,
    )
    if not allowed:
        logger.warning(
            "durable_facts.record_fact blocked by content scanner reason=%s",
            block_reason,
        )
        return None

    # Inbound-content confirmation gate (Fix 3 of 2026-05-06 audit). When the
    # fact text is derived from untrusted inbound content (email body,
    # iMessage text, retrieved web content), we MUST NOT persist it to
    # learned_facts.jsonl on the same turn. v0: log to pending_facts.jsonl
    # and return None. Megha confirms via a tomorrow-task review of pending
    # rows; a future iteration adds the auto-reply confirmation message and
    # the qa_reply "confirm fact <id>" classifier branch.
    if originated_from_inbound_content:
        pending_id = _new_fact_id()
        pending_record = {
            "pending_id": pending_id,
            "ts": _utc_now_iso(),
            "fact_text": text,
            "scope": scope,
            "source_decision_id": source_decision_id,
            "expires_at": expires_at,
            "status": "pending_confirmation",
            "inbound_source": inbound_source or "unspecified",
            "todo": (
                "TODO: Megha to confirm — derived from inbound content; "
                "do not auto-persist. Approve via tomorrow-task review."
            ),
        }
        _append_locked(_pending_facts_path(config), pending_record)
        logger.info(
            "durable_facts.record_fact: inbound-sourced fact queued for "
            "confirmation pending_id=%s scope=%s source=%s",
            pending_id, scope, (inbound_source or "unspecified")[:80],
        )
        try:
            from kavi_runtime.structured_log import log_event
            log_event(
                "durable_facts", "fact_pending",
                inbound_origin=True, scope=scope,
                inbound_source=inbound_source,
            )
        except Exception:
            logger.debug("structured_log fact_pending emit failed (continuing)")
        return None

    fact_id = _new_fact_id()
    record = {
        "fact_id": fact_id,
        "ts": _utc_now_iso(),
        "fact_text": text,
        "scope": scope,
        "source_decision_id": source_decision_id,
        "expires_at": expires_at,
        "status": "active",
    }
    _append_locked(_facts_path(config), record)
    try:
        from kavi_runtime.structured_log import log_event
        log_event(
            "durable_facts", "fact_persisted",
            inbound_origin=False, scope=scope,
            fact_id=fact_id,
        )
    except Exception:
        logger.debug("structured_log fact_persisted emit failed (continuing)")
    return fact_id


def _latest_per_fact(rows: list[dict[str, Any]]) -> "OrderedDict[str, dict[str, Any]]":
    """Collapse the append-only log into the latest row per fact_id. A row's
    `fact_id` is the canonical key; `source_decision_id` on a `superseded` /
    `expired` row points at the prior fact_id it replaces.

    Returns an OrderedDict preserving first-seen insertion order so callers
    can iterate facts in the order they were first recorded.
    """
    latest: OrderedDict[str, dict[str, Any]] = OrderedDict()
    # First pass: index every row by fact_id, keeping the latest by ts.
    for row in rows:
        fid = row.get("fact_id")
        if not fid:
            continue
        prev = latest.get(fid)
        if prev is None:
            latest[fid] = row
            continue
        # Prefer later ts; on tie, prefer non-active (supersede/expire after
        # active is the natural ordering and ts collisions on second
        # resolution are possible).
        if row.get("ts", "") > prev.get("ts", ""):
            latest[fid] = row
        elif row.get("ts", "") == prev.get("ts", "") and row.get("status") != "active":
            latest[fid] = row

    # Second pass: any fact_id that is the target of a supersede/expire row
    # (carried in source_decision_id on those rows) gets its target marked
    # superseded/expired in the latest map.
    for row in rows:
        status = row.get("status")
        if status not in {"superseded", "expired"}:
            continue
        target = row.get("source_decision_id")
        if not target or target not in latest:
            continue
        target_row = latest[target]
        # Only override when the target is currently active; if it was already
        # superseded by a later op, keep that.
        if target_row.get("status") == "active":
            # Mirror the supersede row's status onto the target's latest view
            # so read_active_facts filters it out.
            replaced = dict(target_row)
            replaced["status"] = status
            replaced["superseded_at"] = row.get("ts")
            latest[target] = replaced

    return latest


def read_active_facts(
    scope: str | None = None,
    since: str | None = None,
    *,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Return active facts, optionally filtered.

    Args:
      scope: when set, return only rows whose scope matches.
      since: ISO 8601 UTC timestamp; when set, return only rows whose `ts`
        is strictly greater than `since`.
      config: runtime config dict (optional).

    Returns:
      List of fact dicts (full row shape), insertion-ordered. Excludes any
      fact whose latest row is `superseded` or `expired`, plus any fact
      explicitly targeted by a later supersede/expire op.
    """
    rows = _read_all_locked(_facts_path(config))
    latest = _latest_per_fact(rows)

    out: list[dict[str, Any]] = []
    since_dt = _parse_iso(since) if since else None
    for fid, row in latest.items():
        if row.get("status") != "active":
            continue
        if scope is not None and row.get("scope") != scope:
            continue
        if since_dt is not None:
            try:
                row_dt = _parse_iso(row.get("ts", ""))
            except (ValueError, AttributeError):
                continue
            if row_dt <= since_dt:
                continue
        # Drop the synthetic `superseded_at` if it leaked through (it should
        # only appear on non-active rows).
        clean = {k: v for k, v in row.items() if k != "superseded_at"}
        out.append(clean)
    return out


def supersede_fact(fact_id: str, *, config: dict | None = None) -> None:
    """Mark `fact_id` as superseded by appending a new `superseded` row that
    references the prior fact_id via `source_decision_id`.

    No-op (silently) when `fact_id` is unknown or already superseded/expired.
    Rationale: callers typically want idempotent supersede semantics — if a
    later turn re-supersedes the same fact, we don't want to error or stack
    multiple supersede rows.
    """
    path = _facts_path(config)
    rows = _read_all_locked(path)
    latest = _latest_per_fact(rows)
    target = latest.get(fact_id)
    if target is None:
        logger.info(
            "durable_facts: supersede_fact called on unknown fact_id=%s (no-op)",
            fact_id,
        )
        return
    if target.get("status") != "active":
        logger.info(
            "durable_facts: supersede_fact called on non-active fact_id=%s status=%s (no-op)",
            fact_id,
            target.get("status"),
        )
        return

    record = {
        "fact_id": _new_fact_id(),
        "ts": _utc_now_iso(),
        "fact_text": target.get("fact_text", ""),
        "scope": target.get("scope"),
        "source_decision_id": fact_id,  # points at the fact being replaced
        "expires_at": None,
        "status": "superseded",
    }
    _append_locked(path, record)


def _pending_row_is_fresh(row: dict[str, Any], now: datetime) -> bool:
    """Return True iff a pending_facts.jsonl row has not yet expired.

    Rows whose `expires_at` is missing or null never expire (treated as
    indefinite). Rows with an unparseable `expires_at` are kept (a malformed
    row should not silently vanish from Megha's review).

    The freshness rule is intentionally separate from `_latest_per_fact` /
    `read_active_facts` (which apply to learned_facts.jsonl): pending rows
    are not append-only-with-supersede; they're append-with-status, and the
    only stale signal we have is `expires_at`.
    """
    expires = row.get("expires_at")
    if not expires:
        return True
    try:
        expires_dt = _parse_iso(expires)
    except (ValueError, AttributeError):
        logger.warning(
            "durable_facts: pending row has unparseable expires_at=%r; "
            "treating as fresh and surfacing for Megha's review", expires,
        )
        return True
    return expires_dt >= now


def read_active_pending_facts(
    *, config: dict | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Single read path for pending facts. Returns ONLY rows that are still
    awaiting confirmation AND have not expired.

    Filters applied:
      - status == "pending_confirmation"
      - expires_at is null OR expires_at >= now (`now` defaults to
        datetime.now(timezone.utc))

    Rationale (2026-05-27): the periodic_summary path was reading pending
    rows without checking `expires_at`, which is what burned Megha when
    Kavi re-surfaced five 3-week-old "facts to confirm" at the 9 PM rollup.
    Every read site that feeds pending facts into a user-visible message
    must call this helper so the freshness filter lands in exactly one
    place.

    On any IO error, returns [] and logs. The periodic_summary path
    prefers under-surfacing to crashing the digest.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    path = _pending_facts_path(config)
    try:
        rows = _read_all_locked(path)
    except Exception as e:
        logger.warning(
            "durable_facts.read_active_pending_facts: read failed (continuing empty): %s",
            e,
        )
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "pending_confirmation":
            continue
        if not _pending_row_is_fresh(row, now):
            continue
        out.append(row)
    return out


def prune_expired_pending_facts(
    *, config: dict | None = None, now: datetime | None = None,
) -> int:
    """One-shot GC: rewrite pending_facts.jsonl with expired rows removed.

    Behavior:
      - No-op if the file does not exist (returns 0).
      - No-op if nothing is expired (returns 0; does NOT rewrite, so
        we don't touch the file unnecessarily).
      - On prune: atomic rewrite via tempfile.mkstemp in the same
        directory, fsync, os.replace. The temp name is unique per-writer
        (UUID + pid) — closes the 2026-05-06 shared-`.tmp`-filename
        concurrent-writer corruption pattern.
      - Logs INFO with the count pruned. Returns the count.

    The pruned rows are dropped from disk. There is no `expired` row
    written in pending_facts.jsonl analogous to learned_facts.jsonl,
    because pending rows are review-state, not the durable fact store —
    once their TTL passes Megha is not going to confirm them anyway.

    Called from:
      - server.build_app startup (one-shot at process boot)
      - handlers.periodic_summary (before each digest fire)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    path = _pending_facts_path(config)
    if not path.exists():
        return 0

    # Read under shared lock so we don't race a concurrent writer.
    rows = _read_all_locked(path)
    if not rows:
        return 0

    kept: list[dict[str, Any]] = []
    pruned = 0
    for row in rows:
        # Only prune pending_confirmation rows past expiry. Other-status
        # rows (e.g., confirmed, dropped) are left alone — if downstream
        # code wrote them, that code owns their lifecycle.
        if row.get("status") == "pending_confirmation" and not _pending_row_is_fresh(row, now):
            pruned += 1
            continue
        kept.append(row)

    if pruned == 0:
        return 0

    # Atomic rewrite via state_io.atomic_write_bytes — the single sanctioned
    # primitive for atomic-replace writes runtime-wide (test_state_io_parity
    # enforces this). atomic_write_bytes uses tempfile.mkstemp with a
    # per-writer-unique name in the same directory, fsyncs, and os.replaces.
    # This closes the 2026-05-06 silent-failure pattern (shared `.tmp`
    # filename under concurrent writers corrupting JSON).
    payload = "".join(
        json.dumps(r, separators=(",", ":")) + "\n" for r in kept
    ).encode("utf-8")
    atomic_write_bytes(path, payload)

    logger.info(
        "durable_facts.prune_expired_pending_facts: pruned %d expired row(s) from %s",
        pruned, path,
    )
    try:
        from kavi_runtime.structured_log import log_event
        log_event(
            "durable_facts", "pending_facts_pruned",
            pruned=pruned, path=str(path),
        )
    except Exception:
        logger.debug("structured_log pending_facts_pruned emit failed (continuing)")
    return pruned


def expire_facts(*, config: dict | None = None, now: datetime | None = None) -> int:
    """Sweep facts whose `expires_at` is < now. Appends one `expired` row per
    swept fact. Returns the count of facts swept.

    Args:
      config: runtime config dict (optional).
      now: override "now" for tests; when None, uses datetime.now(UTC).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    path = _facts_path(config)
    rows = _read_all_locked(path)
    latest = _latest_per_fact(rows)

    swept = 0
    for fid, row in latest.items():
        if row.get("status") != "active":
            continue
        expires = row.get("expires_at")
        if not expires:
            continue
        try:
            expires_dt = _parse_iso(expires)
        except (ValueError, AttributeError):
            logger.warning(
                "durable_facts: fact %s has unparseable expires_at=%r; skipping",
                fid,
                expires,
            )
            continue
        if expires_dt >= now:
            continue
        record = {
            "fact_id": _new_fact_id(),
            "ts": _utc_now_iso(),
            "fact_text": row.get("fact_text", ""),
            "scope": row.get("scope"),
            "source_decision_id": fid,
            "expires_at": expires,
            "status": "expired",
        }
        _append_locked(path, record)
        swept += 1
    return swept
