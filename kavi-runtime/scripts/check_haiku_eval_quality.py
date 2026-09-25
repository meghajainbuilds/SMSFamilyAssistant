"""Haiku rollout quality gate (item #4 of 2026-05-06 batch 6).

Two of the seven classifier calls flipped from Sonnet to Haiku today
(`classify_action_intent`; `classify_qa_reply` was deleted 2026-06-10 by
the intent-first dispatch rebuild). The other five are
staged but routed to Sonnet via `model_routing.default` in config.yaml.
Megha will flip those after 24h of clean eval data on the live pair.

This script answers one question: is the live Haiku pair safe to keep,
and are the staged 5 ready to flip?

What it does:
1. Reads runs.jsonl + eval-inbox-judgments.jsonl + eval-persona-action-
   intent.jsonl + any other JSONL with `call_type` rows under the
   configured eval roots.
2. Computes accuracy / pass rate / fall-through rate over the last 24h
   for the two live Haiku call types.
3. Compares to the prior 7-day Sonnet baseline (rows older than 24h).
4. Prints PASS / WATCH / FAIL per call type. Threshold: any single
   accuracy metric down >10% from baseline = FAIL.
5. If both PASS, prints the exact config.yaml diff to flip the
   remaining 5 classifiers.

Usage on Kavi's Mac:
    python -m scripts.check_haiku_eval_quality

Output is human-readable text on stdout. No tests — this is a manual
tool, not a runtime path.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_env  # noqa: E402  (KAVI_HOST from env or kavi-runtime/.deploy.env)

LIVE_HAIKU_CALL_TYPES = ("classify_action_intent",)
STAGED_BUT_NOT_FLIPPED = (
    "classify_correction",
    "classify_self_check_reply",
    "classify_pause_intent",
    "classify_coordination_intent",
    "parse_coordination_reply",
)

# Threshold: a single accuracy/pass/fall-through metric down >10% from
# the 7-day Sonnet baseline = FAIL. Tunable.
FAIL_DROP_PCT = 10.0
WATCH_DROP_PCT = 5.0


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _scan_roots() -> list[Path]:
    """Match the dashboard's scan-roots heuristic so we don't drift."""
    candidates = [
        Path("/Users/kavi/HomeOS/evals"),
        Path("/Users/kavi/HomeOS/metrics"),
        Path("/Users/meghajain/Documents/HomeOS/evals"),
        Path("/Users/meghajain/Documents/HomeOS/metrics"),
    ]
    return [p for p in candidates if p.exists()]


def _iter_rows(roots: list[Path]):
    for root in roots:
        for jsonl in root.rglob("*.jsonl"):
            try:
                f = jsonl.open()
            except OSError:
                continue
            with f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


def _row_call_type(row: dict) -> str | None:
    for k in ("call_type", "kind", "skill"):
        v = row.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _row_model(row: dict) -> str | None:
    m = row.get("model")
    return m if isinstance(m, str) else None


def _row_outcome(row: dict) -> str | None:
    """Bucket each row into one of: correct / fallthrough / parse_error /
    other. We're loose here — the eval JSONL shape varies. The point is a
    single rate per bucket so PASS/WATCH/FAIL is interpretable.

    Heuristics:
      - row.get('decision') == 'correct' or row.get('correct') == True → correct
      - row.get('confidence') == 'low' or status == 'fallthrough' → fallthrough
      - row.get('reason', '') starts with 'parse_error' → parse_error
      - else → other
    """
    if row.get("correct") is True or row.get("decision") == "correct":
        return "correct"
    status = row.get("status")
    if row.get("confidence") == "low" or status == "fallthrough":
        return "fallthrough"
    reason = row.get("reason") or row.get("_error") or ""
    if isinstance(reason, str) and reason.startswith("parse_error"):
        return "parse_error"
    return "other"


def _bucket(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out["total"] += 1
        out[_row_outcome(r) or "other"] += 1
    return dict(out)


def _rate(bucket: dict[str, int], key: str) -> float:
    total = bucket.get("total", 0) or 0
    if total == 0:
        return 0.0
    return 100.0 * bucket.get(key, 0) / total


def _verdict(today: dict[str, int], baseline: dict[str, int]) -> str:
    """Compare key rates today vs baseline. Worst delta wins."""
    if today.get("total", 0) == 0:
        return "WATCH (no data in last 24h)"
    if baseline.get("total", 0) == 0:
        return "WATCH (no baseline data)"
    worst_drop = 0.0
    for key in ("correct", "fallthrough"):
        # `correct` should not drop. `fallthrough` should not rise.
        today_rate = _rate(today, key)
        base_rate = _rate(baseline, key)
        if key == "correct":
            drop = base_rate - today_rate
        else:
            drop = today_rate - base_rate  # rising fallthrough = bad
        if drop > worst_drop:
            worst_drop = drop
    if worst_drop > FAIL_DROP_PCT:
        return f"FAIL (worst metric drift {worst_drop:.1f} pp)"
    if worst_drop > WATCH_DROP_PCT:
        return f"WATCH (worst metric drift {worst_drop:.1f} pp)"
    return f"PASS (worst metric drift {worst_drop:.1f} pp)"


def evaluate(now: datetime | None = None) -> dict[str, dict]:
    now = now or datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    rows_by_call: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"today": [], "baseline": []}
    )

    for row in _iter_rows(_scan_roots()):
        ts = _parse_ts(row.get("ts", ""))
        if not ts:
            continue
        ct = _row_call_type(row)
        if ct not in LIVE_HAIKU_CALL_TYPES:
            continue
        if ts >= cutoff_24h:
            rows_by_call[ct]["today"].append(row)
        elif ts >= cutoff_7d:
            rows_by_call[ct]["baseline"].append(row)

    summary: dict[str, dict] = {}
    for ct in LIVE_HAIKU_CALL_TYPES:
        today_rows = rows_by_call[ct]["today"]
        baseline_rows = rows_by_call[ct]["baseline"]
        today_b = _bucket(today_rows)
        base_b = _bucket(baseline_rows)
        summary[ct] = {
            "today_n": today_b.get("total", 0),
            "baseline_n": base_b.get("total", 0),
            "today_correct_rate": _rate(today_b, "correct"),
            "baseline_correct_rate": _rate(base_b, "correct"),
            "today_fallthrough_rate": _rate(today_b, "fallthrough"),
            "baseline_fallthrough_rate": _rate(base_b, "fallthrough"),
            "verdict": _verdict(today_b, base_b),
        }
    return summary


def render_report(summary: dict[str, dict]) -> str:
    lines = ["Haiku rollout quality gate"]
    lines.append("=" * 60)
    all_pass = True
    for ct, s in summary.items():
        lines.append(f"\n{ct}:")
        lines.append(f"  rows last 24h:   {s['today_n']}")
        lines.append(f"  rows 7d baseline: {s['baseline_n']}")
        lines.append(f"  correct rate:    {s['today_correct_rate']:.1f}% (baseline {s['baseline_correct_rate']:.1f}%)")
        lines.append(f"  fallthrough:     {s['today_fallthrough_rate']:.1f}% (baseline {s['baseline_fallthrough_rate']:.1f}%)")
        lines.append(f"  verdict:         {s['verdict']}")
        if not s["verdict"].startswith("PASS"):
            all_pass = False

    lines.append("")
    if all_pass:
        lines.append("BOTH PASS — safe to flip the remaining 5 classifiers.")
        lines.append("")
        lines.append("config.yaml diff to apply:")
        lines.append("```diff")
        for ct in STAGED_BUT_NOT_FLIPPED:
            lines.append(f"-  # {ct}: claude-haiku-4-5")
            lines.append(f"+  {ct}: claude-haiku-4-5")
        lines.append("```")
        lines.append("After editing config.yaml on Kavi's Mac, restart the runtime:")
        kavi_host = deploy_env.get("KAVI_HOST", "<KAVI_HOST>")
        lines.append(f"  ssh {kavi_host} \"launchctl kickstart -k gui/$(id -u)/com.megha.kavi\"")
    else:
        lines.append("HOLD — at least one call type is WATCH or FAIL.")
        lines.append("Investigate before flipping the remaining 5 classifiers.")
        lines.append("Pull the actual misclassifications from eval JSONLs:")
        lines.append("  evals/inbox-to-task/eval-inbox-judgments.jsonl → grep recent rows")
        lines.append("  evals/kavi-persona/eval-persona-outbound-judgments.jsonl → grep recent rows")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of human report.")
    args = parser.parse_args(argv)

    summary = evaluate()
    if args.json:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render_report(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
