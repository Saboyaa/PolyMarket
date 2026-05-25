from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from polymarket_bot.common.models import (
    Level,
    Market,
    Opportunity,
    OrderBook,
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
