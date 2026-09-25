"""Tests for kavi_runtime.pricing — the ONE canonical home for Anthropic
per-token rates (2026-06-10 dedupe of guardrails / cost dashboard /
error_budget inline pricing math)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kavi_runtime import pricing
from kavi_runtime.pricing import cost_usd, cost_usd_from_usage

# Make scripts/ importable the same way test_cost_dashboard.py does.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import render_cost_dashboard as dash  # type: ignore  # noqa: E402


M = 1_000_000


# ---- known-value checks ------------------------------------------------------


def test_sonnet_input_rate() -> None:
    assert cost_usd("claude-sonnet-4-6", input_tokens=M) == pytest.approx(3.00)


def test_sonnet_output_rate() -> None:
    assert cost_usd("claude-sonnet-4-6", output_tokens=M) == pytest.approx(15.00)


def test_sonnet_cache_write_rate_is_1_25x_input() -> None:
    assert cost_usd("claude-sonnet-4-6", cache_creation=M) == pytest.approx(3.75)
    assert cost_usd("claude-sonnet-4-6", cache_creation=M) == pytest.approx(
        1.25 * cost_usd("claude-sonnet-4-6", input_tokens=M)
    )


def test_sonnet_cache_read_rate_is_0_1x_input() -> None:
    assert cost_usd("claude-sonnet-4-6", cache_read=M) == pytest.approx(0.30)
    assert cost_usd("claude-sonnet-4-6", cache_read=M) == pytest.approx(
        0.1 * cost_usd("claude-sonnet-4-6", input_tokens=M)
    )


def test_sonnet_combined_call_cost() -> None:
    # 1000 in + 200 out + 50K cache-write + 10K cache-read:
    # (1000*3 + 200*15 + 50000*3.75 + 10000*0.30) / 1M = $0.1965
    assert cost_usd(
        "claude-sonnet-4-6",
        input_tokens=1000, output_tokens=200,
        cache_creation=50_000, cache_read=10_000,
    ) == pytest.approx(0.1965)


def test_haiku_rates() -> None:
    assert cost_usd("claude-haiku-4-5", input_tokens=M) == pytest.approx(1.00)
    assert cost_usd("claude-haiku-4-5", output_tokens=M) == pytest.approx(5.00)
    assert cost_usd("claude-haiku-4-5", cache_creation=M) == pytest.approx(1.25)
    assert cost_usd("claude-haiku-4-5", cache_read=M) == pytest.approx(0.10)


def test_unknown_model_prices_at_sonnet_rates() -> None:
    """Routing surprises over-count (trip the cap early) rather than
    under-count."""
    assert cost_usd("claude-mystery-9", input_tokens=M) == pytest.approx(3.00)
    assert cost_usd(None, input_tokens=M) == pytest.approx(3.00)


# ---- usage-dict adapter: both field-name conventions -------------------------


def test_cost_from_usage_accepts_sdk_long_names() -> None:
    usage = {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_creation_input_tokens": 50_000,
        "cache_read_input_tokens": 10_000,
    }
    assert cost_usd_from_usage("claude-sonnet-4-6", usage) == pytest.approx(0.1965)


def test_cost_from_usage_accepts_structured_log_short_names() -> None:
    usage = {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_creation": 50_000, "cache_read": 10_000,
    }
    assert cost_usd_from_usage("claude-sonnet-4-6", usage) == pytest.approx(0.1965)


def test_cost_from_usage_empty_or_none_is_zero() -> None:
    assert cost_usd_from_usage("claude-sonnet-4-6", None) == 0.0
    assert cost_usd_from_usage("claude-sonnet-4-6", {}) == 0.0


# ---- single canonical home: all callers use THIS module ----------------------


def test_guardrails_wrapper_delegates_to_canonical_pricing() -> None:
    from kavi_runtime.runtime import guardrails

    assert guardrails.cost_usd_from_usage is pricing.cost_usd_from_usage
    usage = {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_creation_input_tokens": 50_000,
        "cache_read_input_tokens": 10_000,
    }
    assert guardrails.compute_call_cost_usd(usage) == pytest.approx(
        pricing.cost_usd_from_usage("claude-sonnet-4-6", usage)
    )


def test_cost_dashboard_uses_canonical_pricing() -> None:
    assert dash.cost_usd_from_usage is pricing.cost_usd_from_usage
    assert dash.PRICING_USD_PER_M is pricing.PRICING_USD_PER_M
    usage = {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_creation_input_tokens": 50_000,
        "cache_read_input_tokens": 10_000,
    }
    assert dash._row_cost(usage, "claude-sonnet-4-6") == pytest.approx(0.1965)
    # Haiku rows now price at Haiku rates (previously assumed Sonnet).
    assert dash._row_cost(usage, "claude-haiku-4-5") == pytest.approx(
        pricing.cost_usd_from_usage("claude-haiku-4-5", usage)
    )


def test_error_budget_prices_haiku_rows_at_haiku_rates(tmp_path: Path) -> None:
    """error_budget routes through the canonical module: a row tagged with a
    Haiku model must cost 1/3 the Sonnet input rate, not Sonnet's."""
    import json
    from datetime import datetime, timedelta, timezone

    from kavi_runtime.error_budget import compute_rollup

    now = datetime(2026, 6, 10, 7, 0, tzinfo=timezone.utc)
    log_path = tmp_path / "kavi.json.log"
    log_path.write_text(json.dumps({
        "ts": (now - timedelta(hours=1)).isoformat(),
        "category": "anthropic", "event": "call_done",
        "model": "claude-haiku-4-5",
        "input_tokens": M, "output_tokens": 0,
        "cache_creation": 0, "cache_read": 0,
    }) + "\n")

    rollup = compute_rollup(log_path, now=now, window_hours=24)
    assert rollup["anthropic_spend_usd"] == pytest.approx(1.00)  # Haiku, not $3.00


def test_raw_sdk_usage_shape_with_nested_cache_creation_dict() -> None:
    """Verifier FAIL 2026-06-10 20:18 PT: the raw Anthropic SDK usage dict
    carries cache_creation as a NESTED DICT, not an int. The truthy dict
    short-circuited the `or`, dict * float raised, and the never-raises
    spend hook silently dropped every fresh-cache-write call (the most
    expensive class). This is the exact live shape from the staging log."""
    from kavi_runtime.pricing import cost_usd_from_usage

    usage = {
        "input_tokens": 251,
        "output_tokens": 25,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 27563,
        },
        "cache_creation_input_tokens": 27563,
        "cache_read_input_tokens": 0,
    }
    expected = (251 * 3.00 + 25 * 15.00 + 27563 * 3.75) / 1_000_000
    assert cost_usd_from_usage("claude-sonnet-4-6", usage) == pytest.approx(expected)


def test_nested_dict_without_long_name_sums_dict_values() -> None:
    """If only the nested-dict short name is present, sum its int values."""
    from kavi_runtime.pricing import cost_usd_from_usage

    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 1000,
            "ephemeral_5m_input_tokens": 2000,
        },
    }
    # 5m writes at 1.25x input, 1h writes at 2x input (2026-09-23 fix).
    assert cost_usd_from_usage("claude-sonnet-4-6", usage) == pytest.approx(
        (2000 * 3.75 + 1000 * 6.00) / 1_000_000
    )


# ---- 1h-TTL cache writes (2026-09-23) --------------------------------------


def test_one_hour_cache_writes_bill_at_2x_input() -> None:
    from kavi_runtime.pricing import cost_usd
    # 1M 1h-writes on Sonnet = $6.00 (2x $3), not $3.75.
    assert cost_usd("claude-sonnet-4-6", cache_creation=1_000_000,
                    cache_creation_1h=1_000_000) == pytest.approx(6.00)


def test_mixed_ttl_writes_split_correctly() -> None:
    from kavi_runtime.pricing import cost_usd
    # 600k 5m ($3.75/M) + 400k 1h ($6/M) = 2.25 + 2.40
    assert cost_usd("claude-sonnet-4-6", cache_creation=1_000_000,
                    cache_creation_1h=400_000) == pytest.approx(4.65)


def test_usage_dict_flat_and_nested_1h_fields() -> None:
    from kavi_runtime.pricing import cost_usd_from_usage
    flat = {"cache_creation_input_tokens": 1_000_000, "cache_creation_1h_input_tokens": 1_000_000}
    nested = {"cache_creation_input_tokens": 1_000_000,
              "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000_000}}
    assert cost_usd_from_usage("claude-haiku-4-5", flat) == pytest.approx(2.00)
    assert cost_usd_from_usage("claude-haiku-4-5", nested) == pytest.approx(2.00)
