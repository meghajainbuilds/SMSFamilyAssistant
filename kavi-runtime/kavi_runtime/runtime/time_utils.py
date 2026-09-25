"""Time/latency utilities shared across capabilities."""

from __future__ import annotations

from datetime import datetime, timezone


def _latency_sec(received_iso: str) -> float | None:
    """Seconds from `received_iso` (ISO 8601, Z or +00:00) to now. Returns None
    if the input is empty or unparseable — Graph occasionally returns blanks on
    edge cases."""
    if not received_iso:
        return None
    try:
        received = datetime.fromisoformat(received_iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - received).total_seconds()
    except (ValueError, TypeError):
        return None


__all__ = ["_latency_sec"]
