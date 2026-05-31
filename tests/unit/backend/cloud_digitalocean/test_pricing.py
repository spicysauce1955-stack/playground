"""Tests for the cloud-digitalocean pricing module."""

from __future__ import annotations

import pytest

from playground.backend.cloud_digitalocean.pricing import (
    _HOURS_PER_MONTH,
    DROPLET_PRICES,
    estimate_cost,
)
from playground.planner.plan import CostEstimate


def test_known_size_single_vm_returns_cost_estimate() -> None:
    result = estimate_cost("s-1vcpu-1gb", 1)
    assert isinstance(result, CostEstimate)
    assert result.monthly_usd == 6.0
    assert result.hourly_usd == round(6.0 / _HOURS_PER_MONTH, 5)
    assert result.advisory is True
    assert result.note != ""


def test_unknown_size_returns_none() -> None:
    result = estimate_cost("s-99vcpu-99999gb", 1)
    assert result is None


def test_vm_count_scales_monthly_correctly() -> None:
    result = estimate_cost("s-2vcpu-4gb", 3)
    assert result is not None
    expected_monthly = DROPLET_PRICES["s-2vcpu-4gb"] * 3
    assert result.monthly_usd == expected_monthly


def test_vm_count_scales_hourly_correctly() -> None:
    result = estimate_cost("s-4vcpu-8gb", 2)
    assert result is not None
    expected_monthly = DROPLET_PRICES["s-4vcpu-8gb"] * 2
    expected_hourly = round(expected_monthly / _HOURS_PER_MONTH, 5)
    assert result.hourly_usd == expected_hourly


def test_all_known_sizes_return_estimate() -> None:
    for slug in DROPLET_PRICES:
        result = estimate_cost(slug, 1)
        assert result is not None, f"expected estimate for slug {slug!r}"


def test_advisory_flag_is_always_true() -> None:
    for slug in DROPLET_PRICES:
        result = estimate_cost(slug, 1)
        assert result is not None
        assert result.advisory is True


def test_hourly_rounded_to_5_decimal_places() -> None:
    # s-1vcpu-1gb: 6.0 / 672 = 0.008928571... -> round to 5dp = 0.00893
    result = estimate_cost("s-1vcpu-1gb", 1)
    assert result is not None
    assert result.hourly_usd == round(6.0 / _HOURS_PER_MONTH, 5)
    # Confirm there are at most 5 decimal places in the string representation.
    str_val = str(result.hourly_usd)
    if "." in str_val:
        decimals = str_val.split(".")[1]
        assert len(decimals) <= 5, f"too many decimal places: {result.hourly_usd}"


def test_note_contains_pricing_reference() -> None:
    result = estimate_cost("s-1vcpu-1gb", 1)
    assert result is not None
    assert "digitalocean.com/pricing" in result.note


@pytest.mark.parametrize("size,count,expected_monthly", [
    ("s-1vcpu-1gb", 1, 6.0),
    ("s-1vcpu-2gb", 1, 12.0),
    ("s-2vcpu-2gb", 1, 18.0),
    ("s-2vcpu-4gb", 1, 24.0),
    ("s-4vcpu-8gb", 1, 48.0),
    ("s-1vcpu-1gb", 5, 30.0),
])
def test_price_table_values(size: str, count: int, expected_monthly: float) -> None:
    result = estimate_cost(size, count)
    assert result is not None
    assert result.monthly_usd == expected_monthly
