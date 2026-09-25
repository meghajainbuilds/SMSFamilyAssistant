"""Canonical Anthropic per-token pricing — the ONE place rates live.

Why this module exists (2026-06-10 spend-counter fix): per-model pricing
math previously lived in three places — `runtime/guardrails.py`,
`scripts/render_cost_dashboard.py`, and `error_budget.py` — as separately
hardcoded Sonnet constants. The dashboard's 2026-05-06 docstring records
the first time duplicated spend plumbing produced a $0.00 reading; this
module removes the duplicated-rates half of that failure class. Every
caller (spend counter, monthly cap, /status, 7am error-budget email, cost
dashboard, backfill script) prices through `cost_usd` /
`cost_usd_from_usage`.

Rates are USD per 1M tokens. Cache-write is 1.25x input; cache-read is
0.1x input (Anthropic standard multipliers). Source: Anthropic pricing as
of 2026-06 for the two models routed in config.yaml (`claude-sonnet-4-6`
default, `claude-haiku-4-5` for two classifier call types). If a new
model family is routed, add its row here — unknown models price at Sonnet
rates (the most expensive family in use) so spend is never silently
under-counted.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# USD per 1M tokens, keyed by model-family substring.
PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "sonnet": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,   # 1.25x input
        "cache_read": 0.30,    # 0.1x input
    },
    "haiku": {
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,   # 1.25x input
        "cache_read": 0.10,    # 0.1x input
    },
}

# Unknown / unconfigured models price at Sonnet rates: it is the default
# model in config.yaml AND the most expensive family in use, so a routing
# surprise over-counts spend (trips the cap early) rather than under-counts.
DEFAULT_FAMILY = "sonnet"


def _family(model: str | None) -> str:
    m = (model or "").lower()
    for fam in PRICING_USD_PER_M:
        if fam in m:
            return fam
    if m:
        logger.debug("pricing: unknown model %r; pricing at %s rates", model, DEFAULT_FAMILY)
    return DEFAULT_FAMILY


def cost_usd(
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    cache_creation_1h: int = 0,
) -> float:
    """Cost in USD for one API call's token counts under `model`'s rates.

    `cache_creation` is the TOTAL cache-write count; `cache_creation_1h` is
    the subset written with the 1-hour TTL, which bills at 2x input rather
    than the 5-minute 1.25x (fixed 2026-09-23: every write was priced at
    1.25x, under-counting the email judge by ~$11/month)."""
    rates = PRICING_USD_PER_M[_family(model)]
    one_hour = min(cache_creation_1h or 0, cache_creation or 0)
    five_min = (cache_creation or 0) - one_hour
    return (
        (input_tokens or 0) * rates["input"]
        + (output_tokens or 0) * rates["output"]
        + five_min * rates["cache_write"]
        + one_hour * rates["input"] * 2
        + (cache_read or 0) * rates["cache_read"]
    ) / 1_000_000


def cost_usd_from_usage(model: str | None, usage: dict[str, Any] | None) -> float:
    """Cost in USD from a usage dict. Accepts BOTH field-name conventions:

    - the Anthropic SDK / eval-row shape: `cache_creation_input_tokens` /
      `cache_read_input_tokens`
    - the structured-log `anthropic.call_done` shape written by
      `ClaudeClient._log_call_done`: `cache_creation` / `cache_read`

    The 2026-06-10 investigation found `error_budget.py` reading only the
    long names off rows that carried the short names — silently dropping
    cache-write cost, the dominant component. Normalizing here keeps any
    single reader from re-introducing that mismatch.

    RAW SDK SHAPE (Verifier FAIL, 2026-06-10 20:18 PT): in the raw
    Anthropic SDK usage dict, `cache_creation` is a NESTED DICT
    (`{"ephemeral_5m_input_tokens": N, "ephemeral_1h_input_tokens": M}`),
    not an int — the int lives at `cache_creation_input_tokens`. A truthy
    dict short-circuited the `or`, `dict * float` raised, and the
    never-raises spend hook silently dropped every call with a fresh
    cache write — the most expensive call class. `_tokens()` normalizes:
    ints pass through, dicts sum their values, anything else counts 0.
    """
    if not usage:
        return 0.0

    def _tokens(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, dict):
                return sum(x for x in v.values() if isinstance(x, int))
        return 0

    return cost_usd(
        model,
        input_tokens=_tokens("input_tokens"),
        output_tokens=_tokens("output_tokens"),
        # Long name FIRST: it is always a plain int; the short name can be
        # the raw SDK's nested dict (summed as fallback).
        cache_creation=_tokens("cache_creation_input_tokens", "cache_creation"),
        cache_creation_1h=_one_hour_writes(usage),
        cache_read=_tokens("cache_read_input_tokens", "cache_read"),
    )


__all__ = ["PRICING_USD_PER_M", "DEFAULT_FAMILY", "cost_usd", "cost_usd_from_usage"]


def _one_hour_writes(usage: dict[str, Any]) -> int:
    """1h-TTL cache writes from either a flat `cache_creation_1h_input_tokens`
    field or the raw SDK's nested `cache_creation.ephemeral_1h_input_tokens`."""
    v = usage.get("cache_creation_1h_input_tokens")
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    nested = usage.get("cache_creation")
    if isinstance(nested, dict):
        n = nested.get("ephemeral_1h_input_tokens")
        if isinstance(n, int) and not isinstance(n, bool):
            return n
    return 0
