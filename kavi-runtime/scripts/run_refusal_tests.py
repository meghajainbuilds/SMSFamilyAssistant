#!/usr/bin/env python3
"""Refusal acceptance-criteria test runner (G-O2/G-O3/G-O5).

Loads /Users/kavi/HomeOS/evals/kavi-persona/golden-refusal-cases.json, calls
compose_conversational_reply against each case input, evaluates the reply
against per-case rules, and prints a pass/fail summary.

Each case passes when:
  - reply contains AT LEAST ONE phrase from must_contain_any (case-insensitive)
  - reply contains NONE of must_not_contain_any (case-insensitive)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

GOLDEN_PATH = Path("/Users/kavi/HomeOS/evals/kavi-persona/golden-refusal-cases.json")
CONFIG_PATH = Path("/Users/kavi/kavi-runtime/config.yaml")


def load_env():
    env_text = open("/Users/kavi/.config/kavi/.env").read()
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def evaluate_case(reply: str, case: dict) -> tuple[bool, str]:
    """Returns (passes, reason). Case-insensitive substring match."""
    if reply is None:
        return False, "compose_conversational_reply returned None"
    lower = reply.lower()
    must_any = case.get("must_contain_any", [])
    must_not = case.get("must_not_contain_any", [])

    found_any = any(phrase.lower() in lower for phrase in must_any) if must_any else True
    if not found_any:
        return False, f"missing all of must_contain_any: {must_any}"

    bad_hits = [phrase for phrase in must_not if phrase.lower() in lower]
    if bad_hits:
        return False, f"contains forbidden: {bad_hits}"

    return True, "ok"


def main() -> int:
    load_env()
    import yaml

    config = yaml.safe_load(open(CONFIG_PATH))
    sys.path.insert(0, "/Users/kavi/kavi-runtime")
    from kavi_runtime.claude_client import ClaudeClient

    client = ClaudeClient(config)
    golden = json.load(GOLDEN_PATH.open())
    cases = golden["cases"]

    by_category: dict[str, list[tuple[bool, dict, str, str]]] = {}
    for case in cases:
        try:
            reply = client.compose_conversational_reply(case["input"], recent_outbound=[])
        except Exception as e:
            reply = None
            print(f"[{case['id']}] EXCEPTION: {e}", file=sys.stderr)
        passes, reason = evaluate_case(reply or "", case)
        cat = case["category"]
        by_category.setdefault(cat, []).append((passes, case, reply or "", reason))

    print("=" * 72)
    print("Refusal acceptance-criteria test results")
    print("=" * 72)
    overall_pass = 0
    overall_total = 0
    for cat, results in by_category.items():
        n_pass = sum(1 for p, *_ in results if p)
        n_total = len(results)
        overall_pass += n_pass
        overall_total += n_total
        status = "✓" if n_pass == n_total else "✗"
        print(f"\n{cat.upper()} ({n_pass}/{n_total}) {status}")
        for passes, case, reply, reason in results:
            mark = "✓" if passes else "✗"
            print(f"  {mark} [{case['id']}] {case['input'][:70]}")
            print(f"     reply: {reply[:120]}")
            if not passes:
                print(f"     fail: {reason}")
    print()
    print(f"Overall: {overall_pass}/{overall_total} passing")
    return 0 if overall_pass == overall_total else 1


if __name__ == "__main__":
    sys.exit(main())
