"""Cross-cutting runtime modules — the canonical home for code that more than
one capability depends on.

Established 2026-06-02 as part of the architectural refactor (Phase 4.1).
Capability code lives under `capabilities/<name>/`; cross-cutting plumbing
that two-or-more capabilities both need lives here.

Modules introduced during the Phase 4 physical move (2026-06-02):
  - `clients` — _get_clients singleton triple (Graph, Claude, BlueBubbles)
  - `dedup` — webhook re-fire dedup cache
  - `send_imessage` — _send_imessage_with_fallback + variants (the canonical
    iMessage send wrapper with allowlist + content-scanner gates)
  - `spend_cap` — _check_spend_cap_after_call (post-LLM-call hook)
  - `alerts` — _send_or_queue_alert (quiet-hours-aware)
  - `time_utils` — _latency_sec helper
  - `ids` — _decision_id helper
  - `paths` — _runtime_events_path
"""

from kavi_runtime.runtime import (  # noqa: F401
    alerts,
    clients,
    dedup,
    ids,
    paths,
    send_imessage,
    spend_cap,
    time_utils,
)

__all__ = [
    "alerts",
    "clients",
    "dedup",
    "ids",
    "paths",
    "send_imessage",
    "spend_cap",
    "time_utils",
]
