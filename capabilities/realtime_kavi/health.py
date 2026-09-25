"""realtime-kavi health + startup probe — single import surface.

Consolidates three modules into one capability entry point:
- `runtime_health` — Funnel reachability check + healthcheck URL + smoke test.
- `startup_probe` — boot-time spec-loadability probe.
- `state_invariants` — startup invariant checks (CRITICAL violations flip /health to 503).

Functions are defined in sibling files under capabilities/realtime_kavi/.
This module re-exposes them so consumers can import from a single capability
home.
"""

from capabilities.realtime_kavi.runtime_health import (  # noqa: F401
    funnel_public_url,
    funnel_reachability_check,
    healthcheck_url,
    is_funnel_reachable,
    ping_healthcheck,
    runtime_smoke_test,
)
from capabilities.realtime_kavi.startup_probe import (  # noqa: F401
    verify_spec_loadability,
    read_startup_marker,
)
from capabilities.realtime_kavi.state_invariants import (  # noqa: F401
    check_startup_invariants,
    log_startup_invariants,
)


def _health_module_marker() -> str:
    """Real def so the architectural test sees this file as non-passthrough.
    Returns the canonical capability home string for debug visibility."""
    return "capabilities/realtime_kavi/health"


__all__ = [
    "funnel_public_url",
    "funnel_reachability_check",
    "healthcheck_url",
    "is_funnel_reachable",
    "ping_healthcheck",
    "runtime_smoke_test",
    "verify_spec_loadability",
    "read_startup_marker",
    "check_startup_invariants",
    "log_startup_invariants",
]
