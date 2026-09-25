"""state_io.py — single source of truth for state file I/O.

Every module that persists state under ~/.config/kavi/, kavi-runtime/state/,
or any other state path MUST go through these helpers. Direct calls to
tempfile.mkstemp + os.replace, or path.write_text(json.dumps(...)), are
forbidden outside this module — see tests/test_state_io_parity.py.

Why: HomeOS hit two production silent-failure incidents (2026-05-05,
2026-05-06) where multiple modules each reinvented the atomic-replace
pattern with subtly different correctness. Yesterday's fix patched one
writer; today's incident hit a second writer at a different path. The cure
is one helper, used everywhere, parity-tested.

Three write contracts:
    atomic_write_json   — write JSON or fail; never leave partial bytes.
    atomic_write_text   — same, for text payloads (e.g. MSAL cache).
    atomic_write_bytes  — same, for raw bytes; building block for the others.

Two read contracts:
    read_json_strict    — read or raise; for state where missing == bug.
    read_json_recover   — read; on missing/corrupt, archive + return default;
                          for state where cold-start is acceptable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Write `payload` to `path` atomically.

    Either the new content lands fully, or the file is unchanged.

    Implementation:
      1. Ensure parent directory exists.
      2. Create a per-writer-unique tmp file in the SAME directory (so
         os.replace is atomic across the rename).
      3. Write, fsync, close.
      4. os.replace tmp -> path (atomic on POSIX).

    Cleans up the tmp on failure. Re-raises any underlying I/O error.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: str | Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Write text atomically. See `atomic_write_bytes` for the contract."""
    atomic_write_bytes(path, payload.encode(encoding))


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    indent: int = 2,
    schema: type | None = None,
) -> None:
    """Write JSON atomically. Serialization happens before the tmp file is
    created, so non-serializable data fails before any disk side-effect.

    `schema` (optional): a Pydantic BaseModel class. If provided, validates
    `data` against the schema BEFORE serialization. Validation failures raise
    pydantic.ValidationError at the call site — the file on disk is unchanged.
    Catches shape regressions and missing required fields at write time
    instead of at the next reader's load (P2.7 of audit).
    """
    if schema is not None:
        schema.model_validate(data)
    atomic_write_text(path, json.dumps(data, indent=indent))


def read_json_strict(path: str | Path) -> Any:
    """Read JSON or raise. Use when missing/corrupt state is a bug, not a
    cold-start.

    Raises FileNotFoundError if absent, json.JSONDecodeError if corrupt.
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_json_recover(
    path: str | Path,
    default: Any,
    *,
    archive_corrupt: bool = True,
) -> Any:
    """Read JSON; on missing or corrupt, return `default`.

    On corrupt JSON: log CRITICAL and (if `archive_corrupt`) move the file
    aside as `<path>.corrupt-<utc-ts>` so the next writer doesn't append on
    top of garbage and the next reader sees `default` cleanly.
    """
    target = Path(path)
    if not target.exists():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        if archive_corrupt:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archived = target.with_name(target.name + f".corrupt-{ts}")
            try:
                shutil.move(str(target), str(archived))
                logger.critical(
                    "state_io.read_json_recover: corrupt JSON at %s archived to %s; %s",
                    target, archived, exc,
                )
            except OSError as move_err:
                logger.critical(
                    "state_io.read_json_recover: corrupt JSON at %s and archive failed: %s; %s",
                    target, move_err, exc,
                )
        else:
            logger.critical(
                "state_io.read_json_recover: corrupt JSON at %s; %s",
                target, exc,
            )
        return default
