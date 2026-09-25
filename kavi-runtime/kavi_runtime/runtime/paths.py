"""Cross-cutting eval/event log paths."""

from __future__ import annotations

from pathlib import Path


def _runtime_events_path(config: dict) -> Path:
    """Operational event log: periodic summaries, scheduler-level guardrail trips.
    Not part of the eval surface."""
    return Path(config["paths"]["runtime_events_jsonl"])


__all__ = ["_runtime_events_path"]
