from datetime import date
from decimal import Decimal

from polymarket_bot.common.config import Config, MarketMakingConfig
from polymarket_bot.common.execution import LiveMakerExecutor
from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import MakerOrder, OrderBook, Side

_AS_OF = date(2026, 5, 26)


class FakeMakerClob:
    def __init__(self, trades=None):
        self.posted = []
        self.cancelled = []
        self._trades = trades or []

    def place_maker_order(self, order, token_id):
        self.posted.append((order, token_id))
        return {"orderID": f"x{len(self.posted)}"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"canceled": [order_id]}

    def get_trades(self):
        return self._trades


def _tokens(_cid):
    return ("yes_tok", "no_tok")


def _exec(*, live: bool, armed: bool, clob=None, max_inventory="100"):
    cfg = Config(
        mode="live" if live else "paper",
        mm=MarketMakingConfig(max_inventory=max_inventory),
    )
    return LiveMakerExecutor(
        cfg,
        clob or FakeMakerClob(),
        _tokens,
        "c1",
        "Politics",
        FeeSchedule.default(),
        _AS_OF,
        i_understand_the_risks=armed,
    )


def _bid(size="10", price="0.40"):
    return MakerOrder("c1", Side.YES, buy=True, price=Decimal(price), size=Decimal(size))


def test_disarmed_without_live_mode_refuses():
    clob = FakeMakerClob()
    ex = _exec(live=False, armed=True, clob=clob)
    assert ex.is_armed is False
    out = ex.place(_bid())
    assert out.order_id is None  # unplaced
    assert clob.posted == []  # nothing sent to venue
    assert ex.open_orders == ()


def test_disarmed_without_flag_refuses():
    clob = FakeMakerClob()
    ex = _exec(live=True, armed=False, clob=clob)
    assert ex.is_armed is False
    ex.place(_bid())
    assert clob.posted == []


def test_armed_places_real_order():
    clob = FakeMakerClob()
    ex = _exec(live=True, armed=True, clob=clob)
    assert ex.is_armed is True
    placed = ex.place(_bid())
    assert placed.order_id == "x1"
    assert clob.posted[0][1] == "yes_tok"  # YES side -> yes token
    assert len(ex.open_orders) == 1


def test_refuses_order_that_would_breach_cap():
    clob = FakeMakerClob()
    ex = _exec(live=True, armed=True, clob=clob, max_inventory="5")
    ex.place(_bid(size="10"))  # would take net_yes to 10 > cap 5
    assert clob.posted == []
    assert ex.open_orders == ()


def test_cancel_armed_removes_and_calls_venue():
    clob = FakeMakerClob()
    ex = _exec(live=True, armed=True, clob=clob)
    placed = ex.place(_bid())
    assert ex.cancel(placed.order_id) is True
    assert clob.cancelled == ["x1"]
    assert ex.open_orders == ()
    assert ex.cancel("unknown") is False


def test_reconcile_matches_trades_updates_inventory_and_dedups():
    trades = [{"id": "t1", "order_id": "x1", "size": "10", "price": "0.40", "fee": "0.01"}]
    clob = FakeMakerClob(trades=trades)
    ex = _exec(live=True, armed=True, clob=clob)
    ex.place(_bid(size="10"))

    fills = ex.reconcile(OrderBook("c1"))
    assert len(fills) == 1
    assert ex.inventory.net_yes == Decimal("10")  # bought YES
    assert ex.inventory.fees_paid == Decimal("0.01")
    assert ex.open_orders == ()  # fully filled -> removed

    # same trade seen again -> ignored
    assert ex.reconcile(OrderBook("c1")) == ()


def test_reconcile_partial_fill_keeps_remaining():
    trades = [{"id": "t1", "order_id": "x1", "size": "4", "price": "0.40", "fee": "0.01"}]
    clob = FakeMakerClob(trades=trades)
    ex = _exec(live=True, armed=True, clob=clob)
    ex.place(_bid(size="10"))
    ex.reconcile(OrderBook("c1"))
    assert ex.inventory.net_yes == Decimal("4")
    assert ex.open_orders[0].size == Decimal("6")  # remainder still resting


def test_disarmed_reconcile_and_cancel_are_noops():
    ex = _exec(live=False, armed=True)
    assert ex.reconcile(OrderBook("c1")) == ()
    assert ex.cancel("x1") is False
