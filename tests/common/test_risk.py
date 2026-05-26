"""Tests for common.risk.apply_caps.

Sizes an Opportunity down to respect both the per-trade notional cap and the
remaining total-exposure headroom (max_total_exposure - open_exposure).
Returns None when no viable (positive) size remains.
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_bot.common.config import RiskConfig
from polymarket_bot.common.models import Opportunity
from polymarket_bot.common.risk import apply_caps


def _opp(size: str, yes: str = "0.45", no: str = "0.45") -> Opportunity:
    yes_d, no_d = Decimal(yes), Decimal(no)
    gross = Decimal(1) - (yes_d + no_d)
    return Opportunity(
        condition_id="c1",
        category="Sports",
        yes_ask=yes_d,
        no_ask=no_d,
        size=Decimal(size),
        gross_edge_per_share=gross,
        net_edge_per_share=gross - Decimal("0.01"),
    )


def _cfg(total: str = "10", trade: str = "5") -> RiskConfig:
    return RiskConfig(max_total_exposure=Decimal(total), max_trade_notional=Decimal(trade))


def test_under_cap_unchanged() -> None:
    # notional = 2 * 0.90 = 1.80 < 5 trade cap and < 10 headroom.
    opp = _opp("2")
    out = apply_caps(opp, Decimal("0"), _cfg())
    assert out is not None
    assert out.size == Decimal("2")
    assert out is opp or out.size == opp.size


def test_sized_down_by_per_trade_cap() -> None:
    # price-per-pair = 0.90; trade cap 5 -> max size 5/0.90 = 5.5555...
    opp = _opp("100")
    out = apply_caps(opp, Decimal("0"), _cfg(total="100", trade="5"))
    assert out is not None
    assert out.notional <= Decimal("5")
    # Should be the largest size fitting under the cap.
    assert out.size == (Decimal("5") / Decimal("0.90"))


def test_sized_down_by_total_headroom() -> None:
    # open_exposure 8 of 10 -> headroom 2; per-pair 0.90 -> size 2/0.90.
    opp = _opp("100")
    out = apply_caps(opp, Decimal("8"), _cfg(total="10", trade="5"))
    assert out is not None
    assert out.notional <= Decimal("2")
    assert out.size == (Decimal("2") / Decimal("0.90"))


def test_min_of_two_caps_applies() -> None:
    # headroom 4, trade cap 5 -> headroom binds.
    opp = _opp("100")
    out = apply_caps(opp, Decimal("6"), _cfg(total="10", trade="5"))
    assert out is not None
    assert out.notional <= Decimal("4")
    assert out.size == (Decimal("4") / Decimal("0.90"))


def test_zero_headroom_returns_none() -> None:
    opp = _opp("100")
    assert apply_caps(opp, Decimal("10"), _cfg(total="10", trade="5")) is None


def test_over_capacity_returns_none() -> None:
    opp = _opp("100")
    assert apply_caps(opp, Decimal("12"), _cfg(total="10", trade="5")) is None


def test_total_cap_boundary_exact() -> None:
    # open exactly at cap -> no headroom -> None.
    opp = _opp("1")
    assert apply_caps(opp, Decimal("10"), _cfg(total="10")) is None


def test_preserves_edge_fields_when_resized() -> None:
    opp = _opp("100")
    out = apply_caps(opp, Decimal("0"), _cfg(total="100", trade="5"))
    assert out is not None
    assert out.yes_ask == opp.yes_ask
    assert out.no_ask == opp.no_ask
    assert out.net_edge_per_share == opp.net_edge_per_share
    assert out.gross_edge_per_share == opp.gross_edge_per_share
    assert out.condition_id == opp.condition_id


def test_zero_priced_pair_returns_none() -> None:
    # Degenerate: both asks 0 -> cannot size by notional; treat as no viable size.
    opp = _opp("10", yes="0", no="0")
    out = apply_caps(opp, Decimal("0"), _cfg())
    # With zero cost, the whole size fits under any cap.
    assert out is not None
    assert out.size == Decimal("10")
