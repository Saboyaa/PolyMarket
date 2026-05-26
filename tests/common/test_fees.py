"""Tests for common.fees — the effective-dated taker fee model.

Fee formula (per share): fee_per_share = feeRate * p * (1 - p).
Total leg fee = shares * feeRate * p * (1 - p), USDC, 5-decimal rounding,
minimum 0.00001 when any fee is owed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from polymarket_bot.common.config import FeesConfig
from polymarket_bot.common.fees import (
    MARCH_2026_RATES,
    MARCH_2026_REBATES,
    FeeSchedule,
    leg_fee,
    per_share_fee,
    rebate_rate,
    resolve_fee_rate,
)

# --- rate table / schedule selection -------------------------------------


def test_default_schedule_seeds_march_2026_rates() -> None:
    sched = FeeSchedule.default()
    rates = sched.rates_on(date(2026, 6, 1))
    assert rates["Crypto"] == Decimal("0.07")
    assert rates["Sports"] == Decimal("0.03")
    assert rates["Finance"] == Decimal("0.04")
    assert rates["Politics"] == Decimal("0.04")
    assert rates["Tech"] == Decimal("0.04")
    assert rates["Mentions"] == Decimal("0.04")
    assert rates["Economics"] == Decimal("0.05")
    assert rates["Culture"] == Decimal("0.05")
    assert rates["Weather"] == Decimal("0.05")
    assert rates["Other"] == Decimal("0.05")
    assert rates["Geopolitics"] == Decimal("0")


def test_resolve_fee_rate_is_case_insensitive_and_defaults_to_other() -> None:
    sched = FeeSchedule.default()
    d = date(2026, 6, 1)
    assert resolve_fee_rate("crypto", sched, d) == Decimal("0.07")
    assert resolve_fee_rate("CRYPTO", sched, d) == Decimal("0.07")
    # Unknown category falls back to the Other/General rate.
    assert resolve_fee_rate("SomethingNew", sched, d) == Decimal("0.05")


def test_date_boundary_selects_pre_vs_post_schedule() -> None:
    pre = {"Crypto": Decimal("0.00"), "Other": Decimal("0.00")}
    post = {"Crypto": Decimal("0.07"), "Other": Decimal("0.05")}
    sched = FeeSchedule(
        entries=[
            (date(2020, 1, 1), pre),
            (date(2026, 3, 1), post),
        ]
    )
    # Day before the change -> pre schedule.
    assert sched.rates_on(date(2026, 2, 28))["Crypto"] == Decimal("0.00")
    # Exactly on the effective date -> post schedule (inclusive).
    assert sched.rates_on(date(2026, 3, 1))["Crypto"] == Decimal("0.07")
    # After -> post schedule.
    assert sched.rates_on(date(2026, 4, 1))["Crypto"] == Decimal("0.07")


def test_rates_on_before_any_entry_raises() -> None:
    sched = FeeSchedule(entries=[(date(2026, 3, 1), {"Other": Decimal("0.05")})])
    with pytest.raises(ValueError):
        sched.rates_on(date(2025, 1, 1))


def test_schedule_entries_sorted_regardless_of_input_order() -> None:
    sched = FeeSchedule(
        entries=[
            (date(2026, 3, 1), {"Other": Decimal("0.05")}),
            (date(2020, 1, 1), {"Other": Decimal("0.00")}),
        ]
    )
    assert sched.rates_on(date(2021, 1, 1))["Other"] == Decimal("0.00")
    assert sched.rates_on(date(2026, 6, 1))["Other"] == Decimal("0.05")


def test_empty_schedule_raises() -> None:
    with pytest.raises(ValueError):
        FeeSchedule(entries=[])


# --- config override ------------------------------------------------------


def test_config_override_replaces_rate() -> None:
    sched = FeeSchedule.default()
    cfg = FeesConfig(rate_override={"Crypto": Decimal("0.01")})
    assert resolve_fee_rate("Crypto", sched, date(2026, 6, 1), fees_config=cfg) == Decimal("0.01")
    # Non-overridden categories keep the schedule value.
    assert resolve_fee_rate("Sports", sched, date(2026, 6, 1), fees_config=cfg) == Decimal("0.03")


def test_config_override_case_insensitive() -> None:
    sched = FeeSchedule.default()
    cfg = FeesConfig(rate_override={"crypto": Decimal("0.02")})
    assert resolve_fee_rate("CRYPTO", sched, date(2026, 6, 1), fees_config=cfg) == Decimal("0.02")


def test_empty_override_uses_schedule() -> None:
    sched = FeeSchedule.default()
    cfg = FeesConfig()
    assert resolve_fee_rate("Crypto", sched, date(2026, 6, 1), fees_config=cfg) == Decimal("0.07")


# --- per-share formula ----------------------------------------------------


def test_per_share_fee_value_at_half() -> None:
    # rate 0.04 at p=0.5 -> 0.04 * 0.25 = 0.01
    assert per_share_fee(Decimal("0.04"), Decimal("0.5")) == Decimal("0.01")


def test_per_share_fee_symmetry_30_vs_70() -> None:
    rate = Decimal("0.07")
    assert per_share_fee(rate, Decimal("0.30")) == per_share_fee(rate, Decimal("0.70"))


def test_per_share_fee_zero_rate_is_zero() -> None:
    assert per_share_fee(Decimal("0"), Decimal("0.5")) == Decimal("0")


def test_per_share_fee_at_edges_is_zero() -> None:
    assert per_share_fee(Decimal("0.07"), Decimal("0")) == Decimal("0")
    assert per_share_fee(Decimal("0.07"), Decimal("1")) == Decimal("0")


# --- leg fee: rounding + min fee ------------------------------------------


def test_leg_fee_rounds_to_five_decimals() -> None:
    # shares=1, rate=0.07, p=0.5 -> 0.0175 -> rounds to 0.01750
    fee = leg_fee(Decimal("1"), Decimal("0.07"), Decimal("0.5"))
    assert fee == Decimal("0.01750")
    assert fee.as_tuple().exponent == -5


def test_leg_fee_min_fee_applied_when_positive_but_tiny() -> None:
    # A microscopic owed fee rounds below min -> bumped to 0.00001.
    fee = leg_fee(Decimal("0.0001"), Decimal("0.03"), Decimal("0.5"))
    assert fee == Decimal("0.00001")


def test_leg_fee_zero_when_rate_zero_no_min_applied() -> None:
    # Geopolitics / fee-free: genuinely zero, min fee must NOT apply.
    fee = leg_fee(Decimal("100"), Decimal("0"), Decimal("0.5"))
    assert fee == Decimal("0")


def test_leg_fee_zero_when_size_zero() -> None:
    assert leg_fee(Decimal("0"), Decimal("0.07"), Decimal("0.5")) == Decimal("0")


def test_leg_fee_rounds_half_even_to_five_dp() -> None:
    # shares=2, rate=0.07, p=0.5 -> 2*0.0175 = 0.035 -> 0.03500
    assert leg_fee(Decimal("2"), Decimal("0.07"), Decimal("0.5")) == Decimal("0.03500")


def test_leg_fee_symmetry() -> None:
    a = leg_fee(Decimal("10"), Decimal("0.04"), Decimal("0.3"))
    b = leg_fee(Decimal("10"), Decimal("0.04"), Decimal("0.7"))
    assert a == b


# --- rebate table (data only) ---------------------------------------------


def test_rebate_table_values() -> None:
    assert MARCH_2026_REBATES["Crypto"] == Decimal("0.20")
    assert MARCH_2026_REBATES["Sports"] == Decimal("0.25")
    assert MARCH_2026_REBATES["Geopolitics"] == Decimal("0")


def test_rebate_rate_lookup_case_insensitive_and_default() -> None:
    assert rebate_rate("crypto") == Decimal("0.20")
    assert rebate_rate("Politics") == Decimal("0.25")
    assert rebate_rate("Geopolitics") == Decimal("0")
    # Unknown -> default 25%.
    assert rebate_rate("Whatever") == Decimal("0.25")


def test_march_2026_rates_constant_complete() -> None:
    # Sanity that the seed constant has all spec categories.
    for cat in [
        "Crypto",
        "Sports",
        "Finance",
        "Politics",
        "Tech",
        "Mentions",
        "Economics",
        "Culture",
        "Weather",
        "Other",
        "Geopolitics",
    ]:
        assert cat in MARCH_2026_RATES
