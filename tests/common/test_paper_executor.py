"""Tests for common.execution.paper.PaperExecutor.

Deterministic simulated fills from a provided OrderBook; tracks open_exposure
and realized P&L. When leg 2 (NO) has moved beyond the opportunity's expected
ask, the executor crosses the spread to complete the pair at the next level —
but only if the extra cost stays within max_completion_slippage, else the
result is not completed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from polymarket_bot.common.execution.paper import PaperExecutor
from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import Level, Opportunity, OrderBook

AS_OF = date(2026, 6, 1)


def _book(yes_asks, no_asks, condition_id="c1"):
    return OrderBook(
        condition_id=condition_id,
        yes_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in yes_asks),
        no_asks=tuple(Level(Decimal(p), Decimal(s)) for p, s in no_asks),
    )


def _opp(size="5", yes="0.45", no="0.45", category="Geopolitics", condition_id="c1"):
    yes_d, no_d = Decimal(yes), Decimal(no)
    gross = Decimal(1) - (yes_d + no_d)
    return Opportunity(
        condition_id=condition_id,
        category=category,
        yes_ask=yes_d,
        no_ask=no_d,
        size=Decimal(size),
        gross_edge_per_share=gross,
        net_edge_per_share=gross,
    )


def _exec(book):
    return PaperExecutor(
        book=book,
        fees=FeeSchedule.default(),
        max_completion_slippage=Decimal("0.02"),
        as_of=AS_OF,
    )


def test_clean_fill_both_legs_completed() -> None:
    book = _book([("0.45", "10")], [("0.45", "10")])
    ex = _exec(book)
    res = ex.execute(_opp(size="5"))
    assert res.completed is True
    assert len(res.fills) == 2
    yes_fill = next(f for f in res.fills if f.side.value == "YES")
    no_fill = next(f for f in res.fills if f.side.value == "NO")
    assert yes_fill.size == Decimal("5")
    assert no_fill.size == Decimal("5")
    assert yes_fill.price == Decimal("0.45")
    assert no_fill.price == Decimal("0.45")
    # Geopolitics is fee-free.
    assert res.total_fees == Decimal("0")


def test_open_exposure_accumulates_across_executes() -> None:
    book = _book([("0.45", "100")], [("0.45", "100")])
    ex = _exec(book)
    assert ex.open_exposure == Decimal("0")
    ex.execute(_opp(size="5"))
    # exposure = 5 * (0.45 + 0.45) = 4.50
    assert ex.open_exposure == Decimal("4.50")
    ex.execute(_opp(size="2"))
    assert ex.open_exposure == Decimal("4.50") + Decimal("1.80")


def test_realized_edge_after_fees() -> None:
    # Crypto fee at 0.45: 0.07*0.45*0.55 = 0.017325 per share per leg.
    book = _book([("0.45", "10")], [("0.45", "10")])
    ex = _exec(book)
    res = ex.execute(_opp(size="4", category="Crypto"))
    assert res.completed is True
    # gross edge per share = 0.10; both legs pay Crypto fees, so realized < gross.
    assert res.total_fees > Decimal("0")
    assert res.realized_edge < Decimal("0.10")


def test_leg2_moved_within_slippage_completes_at_next_level() -> None:
    # NO best ask moved to 0.46 (expected 0.45); slippage 0.01 <= 0.02 cap.
    book = _book([("0.45", "10")], [("0.46", "10")])
    ex = _exec(book)
    res = ex.execute(_opp(size="5", no="0.45"))
    assert res.completed is True
    no_fill = next(f for f in res.fills if f.side.value == "NO")
    assert no_fill.price == Decimal("0.46")
    assert "complet" in res.note.lower() or "slippage" in res.note.lower()


def test_leg2_moved_beyond_slippage_not_completed() -> None:
    # NO moved to 0.50 (expected 0.45) -> 0.05 slippage > 0.02 cap.
    book = _book([("0.45", "10")], [("0.50", "10")])
    ex = _exec(book)
    res = ex.execute(_opp(size="5", no="0.45"))
    assert res.completed is False
    # Only YES leg should have filled (the hedge could not be completed safely).
    yes_fills = [f for f in res.fills if f.side.value == "YES"]
    assert len(yes_fills) == 1
    assert res.note != ""


def test_insufficient_yes_depth_partial_then_no_completion() -> None:
    # Only 2 YES shares available though opp wants 5.
    book = _book([("0.45", "2")], [("0.45", "10")])
    ex = _exec(book)
    res = ex.execute(_opp(size="5"))
    # YES fills 2; NO is sized to match the filled YES (2) to stay hedged.
    yes_fill = next(f for f in res.fills if f.side.value == "YES")
    assert yes_fill.size == Decimal("2")
    if res.completed:
        no_fill = next(f for f in res.fills if f.side.value == "NO")
        assert no_fill.size == Decimal("2")


def test_no_yes_liquidity_returns_not_completed() -> None:
    book = _book([], [("0.45", "10")])
    ex = _exec(book)
    res = ex.execute(_opp(size="5"))
    assert res.completed is False
    assert ex.open_exposure == Decimal("0")


def test_exposure_only_counts_filled_shares() -> None:
    book = _book([("0.45", "3")], [("0.45", "10")])
    ex = _exec(book)
    ex.execute(_opp(size="5"))
    # Only 3 pairs filled -> exposure 3 * 0.90.
    assert ex.open_exposure == Decimal("3") * Decimal("0.90")
