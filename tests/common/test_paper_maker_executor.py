from datetime import date
from decimal import Decimal

from polymarket_bot.common.execution import PaperMakerExecutor
from polymarket_bot.common.fees import FeeSchedule, leg_fee, rebate_rate, resolve_fee_rate
from polymarket_bot.common.models import Level, MakerOrder, OrderBook, Side

_AS_OF = date(2026, 5, 26)
_CATEGORY = "Politics"


def _exec() -> PaperMakerExecutor:
    return PaperMakerExecutor("c1", _CATEGORY, FeeSchedule.default(), _AS_OF)


def _rate() -> Decimal:
    return resolve_fee_rate(_CATEGORY, FeeSchedule.default(), _AS_OF)


def _book(*, yes_ask=None, yes_bid=None) -> OrderBook:
    return OrderBook(
        condition_id="c1",
        yes_asks=(Level(Decimal(yes_ask), Decimal("100")),) if yes_ask else (),
        yes_bids=(Level(Decimal(yes_bid), Decimal("100")),) if yes_bid else (),
    )


def test_place_and_cancel():
    ex = _exec()
    o = ex.place(MakerOrder("c1", Side.YES, buy=True, price=Decimal("0.40"), size=Decimal("10")))
    assert o.order_id is not None
    assert len(ex.open_orders) == 1
    assert ex.cancel(o.order_id) is True
    assert ex.open_orders == ()
    assert ex.cancel("nope") is False


def test_resting_bid_fills_when_seller_crosses():
    ex = _exec()
    ex.place(MakerOrder("c1", Side.YES, buy=True, price=Decimal("0.40"), size=Decimal("10")))
    # best ask drops to 0.40 -> our bid fills at our own price
    fills = ex.reconcile(_book(yes_ask="0.40"))
    assert len(fills) == 1 and fills[0].price == Decimal("0.40") and fills[0].size == Decimal("10")
    assert ex.inventory.net_yes == Decimal("10")
    assert ex.inventory.fees_paid > 0 and ex.inventory.rebates_earned > 0
    assert ex.open_orders == ()  # filled order removed


def test_resting_ask_fills_when_buyer_crosses():
    ex = _exec()
    ex.place(MakerOrder("c1", Side.YES, buy=False, price=Decimal("0.60"), size=Decimal("10")))
    fills = ex.reconcile(_book(yes_bid="0.60"))
    assert len(fills) == 1
    assert ex.inventory.net_yes == Decimal("-10")  # sold YES -> net short


def test_no_fill_when_book_does_not_cross():
    ex = _exec()
    ex.place(MakerOrder("c1", Side.YES, buy=True, price=Decimal("0.40"), size=Decimal("10")))
    assert ex.reconcile(_book(yes_ask="0.45")) == ()  # ask above our bid -> rests
    assert ex.inventory.net_yes == Decimal("0")
    assert len(ex.open_orders) == 1


def test_open_exposure_tracks_abs_inventory():
    ex = _exec()
    ex.place(MakerOrder("c1", Side.YES, buy=True, price=Decimal("0.40"), size=Decimal("10")))
    ex.reconcile(_book(yes_ask="0.40"))
    assert ex.open_exposure == Decimal("10")  # |net_yes| * $1


def test_pnl_invariant_flat_round_trip():
    ex = _exec()
    bid, ask, size = Decimal("0.40"), Decimal("0.44"), Decimal("10")
    # buy 10 @ 0.40, then sell 10 @ 0.44 -> flat
    ex.place(MakerOrder("c1", Side.YES, buy=True, price=bid, size=size))
    ex.reconcile(_book(yes_ask="0.40"))
    ex.place(MakerOrder("c1", Side.YES, buy=False, price=ask, size=size))
    ex.reconcile(_book(yes_bid="0.44"))

    inv = ex.inventory
    assert inv.net_yes == Decimal("0")  # flat

    rate = _rate()
    fee = leg_fee(size, rate, bid) + leg_fee(size, rate, ask)
    rebate = (rebate_rate(_CATEGORY) * leg_fee(size, rate, bid)).quantize(Decimal("0.00001")) + (
        rebate_rate(_CATEGORY) * leg_fee(size, rate, ask)
    ).quantize(Decimal("0.00001"))
    expected = (ask - bid) * size - fee + rebate  # spread - fees + rebates

    assert inv.fees_paid == fee
    assert abs(inv.net_pnl - expected) < Decimal("0.00001")
