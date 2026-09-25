"""Structured JSON logging (item #2 of 2026-05-06 hygiene pass).

Why: today the runtime logs to a free-text Python logger only. When something
goes sideways (say, two days of webhook failures), Megha can't grep "show me
every email_arrived event from yesterday" without parsing ad-hoc log lines.
A structured JSON line per high-value event makes downstream rollups (item #6
error budget, future BI / debugging) a one-liner.

What ships:
- `log_event(category, event, **kwargs)` — appends one JSON line to
  `~/Library/Logs/kavi-runtime.json.log` (default; configurable via
  `paths.structured_log` if Megha ever wants to relocate).
- `iter_events(path)` — generator that yields parsed dicts from the log,
  used by item #6 (error-budget tracker) to roll up the previous 24h.

Format (per line):
    {"ts": "<UTC ISO>", "category": "<email|imessage|...>",
     "event": "<verb>", ...kwargs}

This is dual-write: the existing `logging.info / .warning` lines stay
untouched. We never try to replace them, just emit the JSON shape on the
top events. If a JSON write fails for any reason we swallow the error and
log a warning — structured logging must never crash the runtime.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# Runtime env tag stamped on every event. Production sets nothing (default
# "prod"); tests opt in by exporting KAVI_ENV=test. Cost dashboard filters
# rows with env="test" out of spend rollups so test-suite Anthropic mocks
# and fixture data stop polluting prod metrics (2026-05-06 incident:
# fixture data with input_tokens=10 leaked into the live cost dashboard
# during a pytest run against the live runtime).
_RUNTIME_ENV: str = os.environ.get("KAVI_ENV", "prod").lower()


def runtime_env() -> str:
    """Read the current env tag — used by the cost dashboard and any other
    consumer that needs to filter test-vs-prod data."""
    return _RUNTIME_ENV


def set_runtime_env_for_test(env: str) -> None:
    """Test-only: override the env tag for the duration of one test."""
    global _RUNTIME_ENV
    _RUNTIME_ENV = env


VALID_CATEGORIES = frozenset({
    "email",
    "imessage",
    "persona",
    "action",
    "alert",
    "webhook",
    "scheduler",
    "error",
    "anthropic",
    "outbound",
    "durable_facts",
    # Added 2026-05-06 for REC-1 inbox pre-filter SHADOW mode (audits/
    # token_optimization_2026-05-06.md). One event per pre-filter shadow_skip
    # so Megha can grep "show me what would have been skipped today" without
    # opening the audit JSONL. Will graduate from shadow_skip to skip on
    # promote.
    "pre_filter",
})


def default_log_path() -> Path:
    """Default: macOS user logs dir. Caller can override via config."""
    return Path.home() / "Library" / "Logs" / "kavi-runtime.json.log"


def _resolve_path(config: dict | None) -> Path:
    if config:
        configured = config.get("paths", {}).get("structured_log")
        if configured:
            return Path(configured)
    return default_log_path()


# Daily rotation with 30-day retention (added 2026-05-06). Without this,
# the JSON log grows unbounded — at ~3 lines per inbound × ~50 inbounds/day
# that's ~50KB/day or ~18MB/year today, but a chatty future capability
# could push 10x that. `TimedRotatingFileHandler(when='midnight',
# backupCount=30)` rotates once a day and keeps 30 historical files. The
# rotated files get a date suffix like `kavi-runtime.json.log.2026-05-06`.
_HANDLER_CACHE: dict[str, logging.handlers.TimedRotatingFileHandler] = {}
_HANDLER_LOCK = Lock()


def _get_rotating_handler(target: Path) -> logging.handlers.TimedRotatingFileHandler:
    """One TimedRotatingFileHandler per target path, cached process-wide.
    Constructing a fresh handler on every log_event call would (a) leak
    file handles and (b) defeat the rotation logic which keeps state in
    handler instance vars."""
    key = str(target.resolve())
    with _HANDLER_LOCK:
        cached = _HANDLER_CACHE.get(key)
        if cached is not None:
            return cached
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(target),
            when="midnight",
            backupCount=30,
            encoding="utf-8",
            utc=False,  # Pacific-time rotation, matching Megha's mental model
        )
        # Suffix tells the reader which calendar day a rotated file covers.
        handler.suffix = "%Y-%m-%d"
        _HANDLER_CACHE[key] = handler
        return handler


def reset_handlers_for_test() -> None:
    """Tests that exercise rotation must drop the cached handler so they
    don't keep open file handles into other tmp_paths. Production code
    never calls this."""
    with _HANDLER_LOCK:
        for h in _HANDLER_CACHE.values():
            try:
                h.close()
            except Exception:
                pass
        _HANDLER_CACHE.clear()


def log_event(category: str, event: str, *, config: dict | None = None,
              path: Path | None = None, **kwargs: Any) -> None:
    """Emit one JSON line. Never raises — a structured-log failure must
    never take the runtime down."""
    if category not in VALID_CATEGORIES:
        # Soft warn: we'd rather see a misspelled category in the log than
        # silently drop the line. Keep going.
        logger.debug("structured_log: unknown category %r (still writing)", category)

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "event": event,
        "env": _RUNTIME_ENV,
    }
    # Merge kwargs after the canonical fields. If a caller passes ts/category/event
    # in kwargs, they overwrite — caller's responsibility, but normally won't.
    for key, value in kwargs.items():
        record[key] = value

    target = path or _resolve_path(config)
    line = json.dumps(record, default=str)
    try:
        handler = _get_rotating_handler(target)
        # Bypass logging.LogRecord formatting — we already have a finished
        # JSON line and want it written verbatim with a trailing newline.
        # The handler's stream interface gives us controlled rollover +
        # stable file handle.
        if handler.shouldRollover(None):  # type: ignore[arg-type]
            handler.doRollover()
        if handler.stream is None:
            handler.stream = handler._open()
        handler.stream.write(line + "\n")
        handler.stream.flush()
    except Exception:
        logger.warning(
            "structured_log: write failed for category=%s event=%s (continuing)",
            category, event,
        )


def iter_events(path: Path | str):
    """Yield parsed JSON dicts from `path`. Skip malformed lines (defensive).
    Used by item #6 error-budget rollup."""
    p = Path(path)
    if not p.exists():
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.debug("structured_log: skipping malformed line: %.200s", line)
                continue
