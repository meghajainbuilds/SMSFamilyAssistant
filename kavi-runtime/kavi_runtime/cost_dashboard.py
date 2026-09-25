"""Cost-dashboard generator (extracted 2026-05-06 batch 6).

Why this module exists separately from `scripts/render_cost_dashboard.py`:
the script writes HTML to disk for ad-hoc inspection on Kavi's Mac. The
HTTP endpoint at `/runtime-metrics/cost` (server.py) needs the same HTML
served live so Megha can hit it from her laptop over Tailscale without
SSH-ing in. Both surfaces share one generator — the script is a thin
wrapper around `render_dashboard_html()` here.

Cost-cache decision: the dashboard regenerates on each request. The 7-day
JSONL scan reads at most a few MB and completes in under 200ms on Kavi's
Mac, well below any human-noticeable threshold for a metrics page. We
skip the 5-minute cache to keep the implementation simple; if request
volume becomes non-trivial we add `functools.lru_cache(maxsize=1)` with
a TTL wrapper.

This module imports the script (which already lives at runtime). The
script is purely functional — no side effects on import.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _ensure_script_on_path() -> None:
    """Make `scripts/render_cost_dashboard.py` importable as a top-level
    module. The scripts directory has no __init__.py so we add it to
    sys.path on first call."""
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def render_dashboard_html(*, days: int = 7) -> str:
    """Build the full self-contained cost-dashboard HTML for the last
    `days` days. Scans every JSONL file under the configured roots
    (matches the script's behavior). Returns the HTML string ready to
    serve with Content-Type: text/html.

    Failure mode: if no JSONL files are found (fresh install, paths
    misconfigured), returns a minimal HTML stub explaining the state
    rather than raising — the endpoint should never 500 on Megha.
    """
    _ensure_script_on_path()
    import render_cost_dashboard as dash  # type: ignore  # noqa: E402

    jsonl_paths = dash.find_jsonl_paths()
    if not jsonl_paths:
        return _empty_dashboard_html()

    agg = dash.aggregate(jsonl_paths, days=days)
    return dash.render_html(agg, datetime.now(timezone.utc))


def _empty_dashboard_html() -> str:
    """Stub HTML when no JSONL data is found. Keeps the endpoint a 200
    rather than a 500, and tells Megha what to check."""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Kavi runtime — cost dashboard (empty)</title></head>"
        "<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        "max-width:700px;margin:2em auto;padding:0 1em;color:#222;'>"
        "<h1>No cost data found</h1>"
        "<p>The dashboard scans every JSONL file under the configured paths "
        "(per-capability eval surfaces + metrics dir). None matched. "
        "Likely a fresh install or a misconfigured paths.metrics_dir / "
        "paths.eval_*_jsonl entry in config.yaml.</p>"
        "</body></html>"
    )
