"""Token-cost dashboard (item #3 of 2026-05-06 hygiene pass).

Reads the last 7 days of every JSONL file under HomeOS that carries
`usage` token rows (inbox-to-task judgments, persona outbound judgments,
action-intent rows, coordination rows, etc.). Aggregates Anthropic input
+ cache_creation + cache_read + output tokens by day and by call type,
and renders a single self-contained HTML file with inline CSS + inline
SVG sparkline bars.

Why this scans many files (2026-05-06 fix): the prior version pointed at
`runs.jsonl` which is `metrics/runtime-events.jsonl` — that file carries
operational events (periodic_summary, scheduler ticks) but no Anthropic
usage rows, so the dashboard always rendered $0.00 even when real cost
was accruing. The actual usage rows are scattered across the per-
capability eval surfaces (`evals/<capability>/*.jsonl`). We discover all
JSONL files under the configured roots and filter rows that have a
`usage` key with token counts.

Run on Kavi's Mac (where the JSONL files live) or via mounted path:
    python -m scripts.render_cost_dashboard

Output: kavi-runtime/runtime_metrics/cost_dashboard.html
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

# Make the kavi_runtime package importable when running this file directly
# (python scripts/render_cost_dashboard.py) instead of as a module.
_PKG_ROOT = str(Path(__file__).resolve().parents[1])
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# Pricing dedupe (2026-06-10): rates now live ONLY in kavi_runtime.pricing
# (the canonical home shared with guardrails, error_budget, and the spend
# counter). The constants below are re-exported references for the rendered
# HTML's pricing footer; per-row math goes through cost_usd_from_usage,
# which prices Haiku rows correctly instead of assuming Sonnet.
from kavi_runtime.pricing import PRICING_USD_PER_M, cost_usd_from_usage  # noqa: E402

SONNET_INPUT_USD_PER_M = PRICING_USD_PER_M["sonnet"]["input"]
SONNET_OUTPUT_USD_PER_M = PRICING_USD_PER_M["sonnet"]["output"]
SONNET_CACHE_READ_USD_PER_M = PRICING_USD_PER_M["sonnet"]["cache_read"]
SONNET_CACHE_WRITE_USD_PER_M = PRICING_USD_PER_M["sonnet"]["cache_write"]

# Roots under which we scan recursively for JSONL files. Order matters
# only for the "where did we look" footer in the rendered HTML.
KNOWN_SCAN_ROOTS = [
    Path("/Users/kavi/HomeOS/evals"),
    Path("/Users/kavi/HomeOS/metrics"),
    Path("/Users/meghajain/Documents/HomeOS/evals"),
    Path("/Users/meghajain/Documents/HomeOS/metrics"),
]


def _load_config_scan_roots() -> list[Path]:
    """Honor `paths.metrics_dir` and `paths.eval_*_jsonl` keys from runtime
    config if present, returning a deduplicated list of directories to
    scan. Falls back to KNOWN_SCAN_ROOTS when no config is found."""
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    if not config_path.exists():
        return [p for p in KNOWN_SCAN_ROOTS if p.exists()]
    try:
        import yaml  # type: ignore
    except ImportError:
        return [p for p in KNOWN_SCAN_ROOTS if p.exists()]
    try:
        config = yaml.safe_load(config_path.read_text())
    except Exception:
        return [p for p in KNOWN_SCAN_ROOTS if p.exists()]
    paths = (config or {}).get("paths", {})

    roots: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            roots.append(p)

    # Pick up directories that contain JSONL files, derived from any path
    # config keys whose values look like JSONL files.
    for key, value in paths.items():
        if not isinstance(value, str):
            continue
        if value.endswith(".jsonl"):
            parent = Path(value).parent
            if parent.exists():
                _add(parent)

    # Always include the canonical roots if they exist.
    for p in KNOWN_SCAN_ROOTS:
        if p.exists():
            _add(p)
    return roots


def find_jsonl_paths() -> list[Path]:
    """Return every `.jsonl` file under the configured scan roots."""
    roots = _load_config_scan_roots()
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for p in root.rglob("*.jsonl"):
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(p)
    return out


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _row_call_type(row: dict) -> str:
    """Heuristic: prefer explicit `call_type` (item #3 added 2026-05-06),
    else fall back to `event_type` / `kind` / `capability` / `skill`. Each
    eval JSONL uses its own shape — this normalizes them to one bucket
    so the dashboard's by-call-type table is meaningful."""
    for key in ("call_type", "event_type", "kind", "capability", "skill"):
        v = row.get(key)
        if isinstance(v, str) and v:
            return v
    return "unknown"


def _row_cost(usage: dict, model: str | None = None) -> float:
    """Per-row cost via the canonical pricing module. Rows without a model
    field price at Sonnet rates (the prior behavior for every row)."""
    return cost_usd_from_usage(model or "claude-sonnet-4-6", usage)


def aggregate(jsonl_paths: list[Path], days: int = 7) -> dict:
    """Return aggregated stats for the last `days` days, scanning every
    JSONL file in `jsonl_paths`. Rows without a `usage` dict are skipped
    silently — files like metrics/runtime-events.jsonl carry operational
    events that legitimately have no token usage."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_day: dict[str, dict] = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "call_count": 0,
    })
    by_call_type: dict[str, dict] = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "call_count": 0,
    })

    models_seen: set[str] = set()
    total_rows = 0
    rows_with_usage = 0
    files_scanned: list[str] = []

    for runs_path in jsonl_paths:
        if not runs_path.exists():
            continue
        files_scanned.append(str(runs_path))
        try:
            f = runs_path.open()
        except OSError:
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_rows += 1
                # Env filter: skip test-tagged rows so pytest runs against
                # the live runtime don't pollute the prod cost dashboard
                # (2026-05-06 incident). Legacy rows without an `env` field
                # are treated as prod (preserves backward compat with logs
                # written before the env tag landed).
                if row.get("env", "prod") != "prod":
                    continue
                ts = _parse_ts(row.get("ts", ""))
                if not ts or ts < cutoff:
                    continue
                usage = row.get("usage")
                # Tolerate `_usage` (some legacy rows used the underscored shape).
                if not usage or not isinstance(usage, dict):
                    usage = row.get("_usage")
                if not usage or not isinstance(usage, dict):
                    continue
                # Defensive: usage dict needs at least one numeric token field.
                if not any(
                    isinstance(usage.get(k), (int, float))
                    for k in ("input_tokens", "output_tokens",
                              "cache_creation_input_tokens", "cache_read_input_tokens")
                ):
                    continue
                rows_with_usage += 1
                day = ts.date().isoformat()
                call_type = _row_call_type(row)
                model = row.get("model")
                cost = _row_cost(usage, model if isinstance(model, str) else None)
                if isinstance(model, str):
                    models_seen.add(model)

                for bucket in (by_day[day], by_call_type[call_type]):
                    bucket["input_tokens"] += usage.get("input_tokens", 0) or 0
                    bucket["output_tokens"] += usage.get("output_tokens", 0) or 0
                    bucket["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0) or 0
                    bucket["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0) or 0
                    bucket["cost_usd"] += cost
                    bucket["call_count"] += 1

    return {
        "by_day": dict(sorted(by_day.items())),
        "by_call_type": dict(
            sorted(by_call_type.items(), key=lambda kv: -kv[1]["cost_usd"])
        ),
        "total_rows": total_rows,
        "rows_with_usage": rows_with_usage,
        "models_seen": sorted(models_seen),
        "runs_path": ", ".join(files_scanned) if files_scanned else "(no files found)",
        "files_scanned": files_scanned,
    }


def _sparkline_svg(values: list[float], max_value: float, *, width: int = 200,
                   height: int = 24, color: str = "#3b6ea5") -> str:
    """Inline SVG bar chart. Each value renders as a vertical bar; bars are
    spaced evenly across `width`. Plays nice in static HTML, no JS."""
    if not values:
        return ""
    if max_value <= 0:
        max_value = 1.0
    bar_w = max(1.0, width / max(len(values), 1) - 1)
    bars = []
    for i, v in enumerate(values):
        bar_h = max(1.0, (v / max_value) * height) if v > 0 else 0
        x = i * (bar_w + 1)
        y = height - bar_h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" fill="{color}" />'
        )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">{"".join(bars)}</svg>'
    )


def render_html(agg: dict, generated_at: datetime) -> str:
    """Build the full self-contained HTML document."""
    by_day = agg["by_day"]
    by_call_type = agg["by_call_type"]
    total_cost = sum(d["cost_usd"] for d in by_day.values())
    days_present = len(by_day)
    avg_per_day = total_cost / days_present if days_present else 0.0

    daily_costs = [d["cost_usd"] for d in by_day.values()]
    max_daily = max(daily_costs) if daily_costs else 0.0

    model_warning = ""
    seen = agg["models_seen"]
    unknown = [
        m for m in seen
        if not any(fam in m.lower() for fam in PRICING_USD_PER_M)
    ]
    if unknown:
        model_warning = (
            f'<div class="warn">Models seen with no pricing row: {", ".join(escape(m) for m in unknown)}. '
            f"These rows are priced at Sonnet rates — add the model family to kavi_runtime/pricing.py.</div>"
        )

    rows_day = []
    for day, d in by_day.items():
        rows_day.append(
            f"<tr>"
            f"<td>{escape(day)}</td>"
            f"<td>{d['call_count']:,}</td>"
            f"<td>{d['input_tokens']:,}</td>"
            f"<td>{d['cache_creation_input_tokens']:,}</td>"
            f"<td>{d['cache_read_input_tokens']:,}</td>"
            f"<td>{d['output_tokens']:,}</td>"
            f"<td>${d['cost_usd']:.4f}</td>"
            f"<td>{_sparkline_svg([d['cost_usd']], max_daily)}</td>"
            f"</tr>"
        )

    rows_type = []
    for ct, d in by_call_type.items():
        rows_type.append(
            f"<tr>"
            f"<td>{escape(ct)}</td>"
            f"<td>{d['call_count']:,}</td>"
            f"<td>{d['input_tokens']:,}</td>"
            f"<td>{d['cache_creation_input_tokens']:,}</td>"
            f"<td>{d['cache_read_input_tokens']:,}</td>"
            f"<td>{d['output_tokens']:,}</td>"
            f"<td>${d['cost_usd']:.4f}</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Kavi runtime — token cost dashboard</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #222; }}
  h1 {{ margin-bottom: 0.2em; }}
  .meta {{ color: #666; font-size: 0.9em; margin-bottom: 1.5em; }}
  .summary {{ background: #f6f8fa; padding: 1em 1.2em; border-radius: 6px; margin-bottom: 1.5em; }}
  .summary div {{ margin: 0.2em 0; }}
  .summary .big {{ font-size: 1.4em; font-weight: 600; }}
  .warn {{ background: #fff4e0; padding: 0.8em 1em; border-left: 4px solid #d97706; margin-bottom: 1em; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2em; font-size: 0.92em; }}
  th, td {{ text-align: right; padding: 6px 10px; border-bottom: 1px solid #eee; }}
  th:first-child, td:first-child {{ text-align: left; }}
  th {{ background: #f0f3f7; font-weight: 600; }}
  td.right {{ text-align: right; }}
  h2 {{ margin-top: 2em; }}
</style>
</head>
<body>
<h1>Kavi runtime — token cost dashboard</h1>
<div class="meta">Generated {escape(generated_at.strftime("%Y-%m-%d %H:%M UTC"))} from {escape(agg["runs_path"])}. Pricing: Sonnet ${SONNET_INPUT_USD_PER_M}/M input, ${SONNET_OUTPUT_USD_PER_M}/M output, ${SONNET_CACHE_READ_USD_PER_M}/M cache read, ${SONNET_CACHE_WRITE_USD_PER_M}/M cache write.</div>

{model_warning}

<div class="summary">
  <div class="big">Last 7 days: ${total_cost:.2f}</div>
  <div>Avg per day (days seen): ${avg_per_day:.2f}</div>
  <div>Days with activity: {days_present}</div>
  <div>Total run rows: {agg["total_rows"]:,} ({agg["rows_with_usage"]:,} with usage)</div>
  <div>Models seen: {", ".join(escape(m) for m in seen) if seen else "(none)"}</div>
</div>

<h2>Daily breakdown</h2>
<table>
<thead><tr><th>Day (UTC)</th><th>Calls</th><th>Input</th><th>Cache write</th><th>Cache read</th><th>Output</th><th>Cost</th><th>Bar</th></tr></thead>
<tbody>
{"".join(rows_day) if rows_day else '<tr><td colspan="8">No data in window.</td></tr>'}
</tbody>
</table>

<h2>By call type</h2>
<table>
<thead><tr><th>Call type</th><th>Calls</th><th>Input</th><th>Cache write</th><th>Cache read</th><th>Output</th><th>Cost</th></tr></thead>
<tbody>
{"".join(rows_type) if rows_type else '<tr><td colspan="7">No data in window.</td></tr>'}
</tbody>
</table>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-path", type=Path, default=None,
                        help="Override scan: a single JSONL file. Defaults to scanning all "
                             "JSONL files under configured paths.")
    parser.add_argument("--scan-root", type=Path, action="append", default=None,
                        help="Override scan: a directory to recurse for *.jsonl. "
                             "Repeatable. Defaults to configured roots.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output HTML path. Defaults to runtime_metrics/cost_dashboard.html.")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    if args.runs_path:
        jsonl_paths = [args.runs_path]
    elif args.scan_root:
        jsonl_paths = []
        seen: set[Path] = set()
        for root in args.scan_root:
            for p in Path(root).rglob("*.jsonl"):
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    jsonl_paths.append(p)
    else:
        jsonl_paths = find_jsonl_paths()

    if not jsonl_paths:
        print("error: no JSONL files found at any known location", file=sys.stderr)
        return 2

    out_path = args.out or (
        Path(__file__).resolve().parents[1] / "runtime_metrics" / "cost_dashboard.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    agg = aggregate(jsonl_paths, days=args.days)
    html = render_html(agg, datetime.now(timezone.utc))
    out_path.write_text(html)
    print(
        f"wrote {out_path} ({len(html):,} bytes, "
        f"{agg['rows_with_usage']:,} rows w/ usage, {len(jsonl_paths)} files scanned)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
