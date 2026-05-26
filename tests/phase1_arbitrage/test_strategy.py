"""Tests for phase1_arbitrage.strategy.find_intramarket_arb.

Net edge per share over both-side consumable depth, minus per-share taker
fees on both legs. A sized Opportunity is returned only when
net_edge_per_share >= min_net_edge_per_share, else None.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import Level, OrderBook
from polymarket_bot.phase1_arbitrage.strategy import find_intramarket_arb

AS_OF = date(2026, 6, 1)


def _book(
    yes_asks: list[tuple[str, str]],
    no_asks: list[tuple[str, str]],
    category: str = "Sports",
    condition_id: str = "c1",
) -> OrderBook:
    return OrderBook(
        condition_id=condition_id,
        yes_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in yes_asks),
        no_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in no_asks),
    )


def _sched() -> FeeSchedule:
    return FeeSchedule.default()


def _find(book: OrderBook, category: str, threshold: str = "0.005"):
    return find_intramarket_arb(book, _sched(), Decimal(threshold), category=category, as_of=AS_OF)


def test_positive_edge_returns_sized_opportunity() -> None:
    # Sports rate 0.03. yes 0.45, no 0.45 -> gross 0.10.
    # per-share fee each leg = 0.03*0.45*0.55 = 0.007425; both legs 0.01485.
    # net ~ 0.08515 >> threshold.
    book = _book([("0.45", "10")], [("0.45", "8")], category="Sports")
    opp = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Sports", as_of=AS_OF)
    assert opp is not None
    assert opp.condition_id == "c1"
    assert opp.category == "Sports"
    assert opp.yes_ask == Decimal("0.45")
    assert opp.no_ask == Decimal("0.45")
    # Size limited by the smaller side depth (8).
    assert opp.size == Decimal("8")
    assert opp.gross_edge_per_share == Decimal("0.10")
    # net = gross - sum(per-share fee both legs)
    expected_fee = Decimal("0.03") * Decimal("0.45") * Decimal("0.55") * 2
    assert opp.net_edge_per_share == Decimal("0.10") - expected_fee


def test_no_arb_when_asks_sum_above_one() -> None:
    book = _book([("0.60", "10")], [("0.55", "10")])
    opp = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Sports", as_of=AS_OF)
    assert opp is None


def test_fees_eat_edge_returns_none() -> None:
    # Crypto 0.07, thin gross edge that fees erase.
    # yes 0.49 no 0.50 -> gross 0.01.
    # fee each ~0.07*0.49*0.51=0.017493 + 0.07*0.5*0.5=0.0175 -> >0.03 total.
    book = _book([("0.49", "10")], [("0.50", "10")], category="Crypto")
    opp = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Crypto", as_of=AS_OF)
    assert opp is None


def test_asymmetric_depth_caps_size_to_smaller_side() -> None:
    book = _book([("0.40", "3")], [("0.40", "100")], category="Sports")
    opp = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Sports", as_of=AS_OF)
    assert opp is not None
    assert opp.size == Decimal("3")


def test_exactly_at_threshold_is_included() -> None:
    # Geopolitics is fee-free, so net edge == gross edge.
    # Choose gross edge exactly equal to threshold 0.005.
    book = _book([("0.50", "5")], [("0.495", "5")], category="Geopolitics")
    opp = _find(book, "Geopolitics")
    assert opp is not None
    assert opp.net_edge_per_share == Decimal("0.005")


def test_just_below_threshold_returns_none() -> None:
    book = _book([("0.50", "5")], [("0.496", "5")], category="Geopolitics")
    opp = _find(book, "Geopolitics")
    assert opp is None


def test_empty_book_returns_none() -> None:
    book = _book([], [], category="Sports")
    assert _find(book, "Sports") is None


def test_one_side_empty_returns_none() -> None:
    book = _book([("0.40", "5")], [], category="Sports")
    assert _find(book, "Sports") is None


def test_uses_best_top_of_book_prices() -> None:
    # Multiple levels; best ask is the lowest price (first level).
    book = _book(
        [("0.40", "5"), ("0.42", "10")],
        [("0.45", "5"), ("0.50", "10")],
        category="Sports",
    )
    opp = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Sports", as_of=AS_OF)
    assert opp is not None
    assert opp.yes_ask == Decimal("0.40")
    assert opp.no_ask == Decimal("0.45")
    # consumable depth at the best level: min(5, 5) = 5
    assert opp.size == Decimal("5")


def test_category_defaults_to_book_market_category_argument() -> None:
    # The strategy reads category from the explicit argument; verify it flows
    # into fee selection (Crypto vs Sports differ).
    book = _book([("0.45", "10")], [("0.45", "10")])
    sports = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Sports", as_of=AS_OF)
    crypto = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Crypto", as_of=AS_OF)
    assert sports is not None and crypto is not None
    assert crypto.net_edge_per_share < sports.net_edge_per_share


def test_zero_size_levels_yield_none() -> None:
    book = _book([("0.40", "0")], [("0.40", "10")], category="Sports")
    opp = find_intramarket_arb(book, _sched(), Decimal("0.005"), category="Sports", as_of=AS_OF)
    assert opp is None
