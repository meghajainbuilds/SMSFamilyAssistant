"""Spec-loadability startup probe — the drift alarm for the
spec-IS-the-runtime architecture.

Why this module exists. Commit `f6ff560` (2026-05-27) and commit `6cd682d`
(2026-05-28) made two canonical spec files runtime-loaded:

  * `capabilities/kavi-persona.md`  → `kavi_runtime/persona_loader.py`
  * `capabilities/security-baseline.md` → `kavi_runtime/security_baseline.py`

Edits to those specs now flow into every composer call automatically. The
upside is no more spec-vs-runtime drift. The downside is a new silent
failure class: if either file goes missing on Kavi (rsync drop, manual
edit on the box, future loader-regression that fails extraction), the
runtime composes voiceless or boundary-less output until somebody
notices. The two loaders already raise loudly *at first composer call*,
but a composer call only happens when Megha texts Kavi — which means
discovery delay is hours-to-days.

This probe runs at process startup, BEFORE the HTTP server accepts
webhooks. If either spec fails to load, the runtime halts with a non-
zero exit, writes a structured failure marker, and launchctl logs the
error. The `/status` endpoint reads the marker on subsequent restarts
so Megha can see "ok=true with hashes" or "ok=false with failure list"
from her phone.

Contract:

  * Single public function, `verify_spec_loadability(config)`.
  * Calls both loaders against the configured paths. Treats either-side
    failure (FileNotFoundError, ValueError, anything else) as a probe
    fail. Also treats "loaded but suspiciously short text" (<500 chars)
    as a fail; the real specs are ~15 KB each, so anything that small is
    a sign the loader silently returned an empty section.
  * On any failure: writes the failure marker atomically, logs a
    CRITICAL line, and raises SystemExit(1). The probe is on the hot
    startup path — failing fast is the point.
  * On success: writes a `{ok: true, ts, persona_hash, security_hash}`
    marker atomically so `/status` can surface "spec loaders OK at <ts>"
    on the next page render.

Atomic marker writes go through `kavi_runtime.state_io.atomic_write_json`
so the marker file is never half-written. Same pattern used everywhere
else state lands on disk (state_io is the single source of truth, per
2026-05-06 state-file audit).
"""

from __future__ import annotations

import hashlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.persona_loader import load_persona_text
from kavi_runtime.security_baseline import load_security_baseline_text
from kavi_runtime.state_io import atomic_write_json

logger = logging.getLogger(__name__)


# Minimum text length the loader must return before we treat the spec as
# "really loaded." The real persona spec is ~15 KB and the security
# baseline is ~12 KB. A loader that silently returned an empty Behavior /
# System prompt section would return on the order of tens of bytes, not
# 500. Threshold sits well below both real-spec sizes and well above any
# "loader returned a header only" failure mode.
_MIN_SPEC_TEXT_BYTES = 500

# Default marker path. Kavi's runtime state lives under
# /Users/kavi/HomeOS/state/ (see config.yaml paths.imessage_state for the
# canonical state-dir example). The marker is operational telemetry, not
# user content — same security posture as the rest of HomeOS/state/.
_DEFAULT_MARKER_PATH = "/Users/kavi/HomeOS/state/last_startup.json"


def _marker_path(config: dict) -> Path:
    """Resolve the marker path. Defaults to the canonical HomeOS state
    directory; tests override via `config.paths.startup_marker`."""
    raw = (config.get("paths") or {}).get("startup_marker") or _DEFAULT_MARKER_PATH
    return Path(raw)


def _hash_text(text: str) -> str:
    """SHA256-prefix of the loaded spec text. 16 hex chars is enough to
    spot drift across deploys when surfaced on the /status page; the
    full hash would clutter the line for no observable gain."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_marker(marker: Path, payload: dict[str, Any]) -> None:
    """Best-effort atomic write of the startup marker. A marker-write
    failure must not mask the probe's own outcome — the probe's loud
    SystemExit is what halts startup, not the marker. We log and move on
    if disk is the problem (e.g., a read-only mount during recovery)."""
    try:
        atomic_write_json(marker, payload)
    except Exception as exc:  # noqa: BLE001
        logger.critical(
            "startup_probe: failed to write marker at %s: %s", marker, exc
        )


def verify_spec_loadability(config: dict) -> None:
    """Halt startup if either canonical spec fails to load.

    Called from `kavi_runtime.main.run()` AFTER config load but BEFORE
    the HTTP server starts accepting webhooks. On failure, writes a
    `{ok: false, ts, failures}` marker, logs CRITICAL, and raises
    SystemExit(1) so launchctl surfaces the exit non-zero and the
    process does not silently accept inbound traffic on broken specs.

    On success, writes a `{ok: true, ts, persona_hash, security_hash}`
    marker so the `/status` endpoint can render "spec loaders OK".

    Args:
      config: parsed `config.yaml`. Reads
        `paths.kavi_persona_md`, `paths.security_baseline_md`, and
        (optionally) `paths.startup_marker`.

    Raises:
      SystemExit: with code 1 when either spec fails to load or returns
        suspiciously short text. The traceback-equivalent reason is on
        the marker file + the CRITICAL log line.
    """
    paths = config.get("paths") or {}
    persona_path = paths.get("kavi_persona_md")
    security_path = paths.get("security_baseline_md")

    failures: list[str] = []
    persona_text: str | None = None
    security_text: str | None = None

    # ---- Persona spec ----------------------------------------------------
    try:
        if not persona_path:
            failures.append(
                "persona spec path not configured "
                "(paths.kavi_persona_md missing from config.yaml)"
            )
        else:
            persona_text = load_persona_text(persona_path)
            if not persona_text or len(persona_text) < _MIN_SPEC_TEXT_BYTES:
                failures.append(
                    "persona spec loaded but suspiciously short "
                    f"({len(persona_text or '')} chars; threshold "
                    f"{_MIN_SPEC_TEXT_BYTES})"
                )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"persona spec load failed: {exc}")

    # ---- Security baseline spec -----------------------------------------
    try:
        if not security_path:
            failures.append(
                "security baseline path not configured "
                "(paths.security_baseline_md missing from config.yaml)"
            )
        else:
            security_text = load_security_baseline_text(security_path)
            if not security_text or len(security_text) < _MIN_SPEC_TEXT_BYTES:
                failures.append(
                    "security baseline loaded but suspiciously short "
                    f"({len(security_text or '')} chars; threshold "
                    f"{_MIN_SPEC_TEXT_BYTES})"
                )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"security baseline load failed: {exc}")

    marker = _marker_path(config)

    if failures:
        payload: dict[str, Any] = {
            "ok": False,
            "ts": _now_iso(),
            "failures": failures,
        }
        _write_marker(marker, payload)
        # CRITICAL log so launchctl + the structured tail catch it. The
        # SystemExit message goes to stderr too, where launchd records it.
        for line in failures:
            logger.critical("startup_probe: %s", line)
        raise SystemExit(
            "DRIFT ALARM: spec load failed.\n" + "\n".join(failures)
        )

    # Happy path. Persist hashes so `/status` can spot drift across
    # deploys by eye even without a diff tool.
    payload = {
        "ok": True,
        "ts": _now_iso(),
        "persona_hash": _hash_text(persona_text or ""),
        "security_hash": _hash_text(security_text or ""),
    }
    _write_marker(marker, payload)
    logger.info(
        "startup_probe: spec loaders OK (persona=%s security=%s)",
        payload["persona_hash"],
        payload["security_hash"],
    )


def read_startup_marker(config: dict) -> dict[str, Any] | None:
    """Read the most recent startup marker. Used by the `/status`
    endpoint to surface `spec_loaders_ok` + `spec_loaders_checked_at`.

    Returns:
      The marker dict, or None when the file is missing or malformed.
      Returning None (rather than raising) lets `/status` render
      `spec_loaders_ok: unknown` without crashing the page.
    """
    marker = _marker_path(config)
    if not marker.exists():
        return None
    try:
        import json
        return json.loads(marker.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
