"""Unique identifier generators shared across capabilities."""

from __future__ import annotations

from kavi_runtime.state import utc_now_iso


def _decision_id(email_id: str | None) -> str:
    """Decision row identifier: timestamp + short suffix from email_id. Unique per
    decision because timestamps include milliseconds."""
    suffix = (email_id or "noid")[:16]
    return f"d_{utc_now_iso()}_{suffix}"


__all__ = ["_decision_id"]
