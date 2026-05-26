from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polymarket_bot.common.models import (
    InventoryState,
    Level,
    MakerOrder,
    Market,
    Opportunity,
    OrderBook,
    Quote,
    Side,
)


def test_level_validates_price_range():
    with pytest.raises(ValueError):
        Level(price=Decimal("1.5"), size=Decimal("10"))
    with pytest.raises(ValueError):
        Level(price=Decimal("0.5"), size=Decimal("-1"))


def test_orderbook_best_ask():
    book = OrderBook(
        condition_id="c1",
        yes_asks=(Level(Decimal("0.40"), Decimal("100")), Level(Decimal("0.42"), Decimal("50"))),
        no_asks=(Level(Decimal("0.55"), Decimal("80")),),
    )
    assert book.best_ask(Side.YES).price == Decimal("0.40")
    assert book.best_ask(Side.NO).price == Decimal("0.55")


def test_best_ask_empty_returns_none():
    assert OrderBook(condition_id="c1").best_ask(Side.YES) is None


def test_opportunity_notional_and_profit():
    opp = Opportunity(
        condition_id="c1",
        category="Politics",
        yes_ask=Decimal("0.40"),
        no_ask=Decimal("0.55"),
        size=Decimal("10"),
        gross_edge_per_share=Decimal("0.05"),
        net_edge_per_share=Decimal("0.03"),
    )
    assert opp.notional == Decimal("9.50")
    assert opp.expected_profit == Decimal("0.30")


def test_market_is_frozen():
    m = Market("c1", "Q?", "Politics", "y", "n")
    with pytest.raises(FrozenInstanceError):
        m.question = "changed"  # type: ignore[misc]


def test_market_end_date_defaults_none_and_holds_datetime():
    assert Market("c1", "Q?", "Politics", "y", "n").end_date is None
    end = datetime(2026, 12, 31, tzinfo=UTC)
    assert Market("c1", "Q?", "Politics", "y", "n", end_date=end).end_date == end


def test_orderbook_best_bid_and_mid():
    book = OrderBook(
        condition_id="c1",
        yes_asks=(Level(Decimal("0.42"), Decimal("100")),),
        yes_bids=(Level(Decimal("0.40"), Decimal("100")), Level(Decimal("0.38"), Decimal("50"))),
    )
    assert book.best_bid(Side.YES).price == Decimal("0.40")
    assert book.mid_price(Side.YES) == Decimal("0.41")
    assert book.best_bid(Side.NO) is None
    assert book.mid_price(Side.NO) is None  # missing a side -> None


def test_quote_invariants():
    q = Quote(condition_id="c1", bid=Decimal("0.40"), ask=Decimal("0.44"), size=Decimal("10"))
    assert q.mid == Decimal("0.42")
    assert q.half_spread == Decimal("0.02")
    with pytest.raises(ValueError):  # crosses
        Quote(condition_id="c1", bid=Decimal("0.45"), ask=Decimal("0.44"), size=Decimal("10"))
    with pytest.raises(ValueError):  # out of (0,1)
        Quote(condition_id="c1", bid=Decimal("0"), ask=Decimal("0.44"), size=Decimal("10"))
    with pytest.raises(ValueError):  # nonpositive size
        Quote(condition_id="c1", bid=Decimal("0.40"), ask=Decimal("0.44"), size=Decimal("0"))


def test_maker_order_invariants():
    o = MakerOrder("c1", Side.YES, buy=True, price=Decimal("0.40"), size=Decimal("10"))
    assert o.order_id is None and o.buy is True
    with pytest.raises(ValueError):
        MakerOrder("c1", Side.YES, buy=True, price=Decimal("1"), size=Decimal("10"))


def test_inventory_state_pnl_and_flat():
    inv = InventoryState("c1")
    assert inv.is_flat and inv.net_pnl == Decimal("0")
    inv = InventoryState(
        "c1",
        net_yes=Decimal("5"),
        realized_pnl=Decimal("1.00"),
        fees_paid=Decimal("0.20"),
        rebates_earned=Decimal("0.05"),
    )
    assert inv.is_flat is False
    assert inv.net_pnl == Decimal("0.85")  # 1.00 - 0.20 + 0.05
