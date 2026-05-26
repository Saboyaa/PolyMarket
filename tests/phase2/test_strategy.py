from decimal import Decimal

from polymarket_bot.common.config import MarketMakingConfig
from polymarket_bot.common.models import InventoryState, Level, OrderBook
from polymarket_bot.phase2_market_making.strategy import build_quotes, quote_cadence_seconds


def _book(mid: str, spread: str = "0.02", cid: str = "c1") -> OrderBook:
    m, s = Decimal(mid), Decimal(spread)
    return OrderBook(
        condition_id=cid,
        yes_asks=(Level(m + s / 2, Decimal("100")),),
        yes_bids=(Level(m - s / 2, Decimal("100")),),
    )


def _cfg(**kw):
    base = dict(
        base_spread="0.005",
        k_gamma="0.1",
        k_inv="0.001",
        sigma="0.1",
        max_inventory="100",
        quote_size="10",
        min_hours_to_resolution="1",
        gamma_ceiling="100",
        tick_size="0.0001",
    )
    base.update(kw)
    return MarketMakingConfig(**base)


def _flat() -> InventoryState:
    return InventoryState("c1")


def test_no_quote_without_mid():
    # only asks, no bids -> no mid -> no quote
    book = OrderBook(condition_id="c1", yes_asks=(Level(Decimal("0.5"), Decimal("10")),))
    assert build_quotes(book, 24 * 30, 0.1, _flat(), _cfg()) is None


def test_quotes_straddle_reservation_price_when_flat():
    q = build_quotes(_book("0.50"), 24 * 30, 0.1, _flat(), _cfg())
    assert q is not None
    assert q.bid < q.ask  # not crossed
    assert abs(q.mid - Decimal("0.50")) <= _cfg().tick_size  # centred on mid when flat


def test_skew_moves_centre_against_inventory():
    cfg = _cfg(k_inv="0.001")
    long = InventoryState("c1", net_yes=Decimal("50"))  # skew = 0.05
    short = InventoryState("c1", net_yes=Decimal("-50"))
    q_long = build_quotes(_book("0.50"), 24 * 30, 0.1, long, cfg)
    q_short = build_quotes(_book("0.50"), 24 * 30, 0.1, short, cfg)
    assert q_long.mid < Decimal("0.50")  # long -> quote lower (encourage selling)
    assert q_short.mid > Decimal("0.50")  # short -> quote higher


def test_spread_widens_monotonically_as_resolution_nears():
    cfg = _cfg()
    halves = [
        build_quotes(_book("0.50"), h, 0.1, _flat(), cfg).half_spread
        for h in (24 * 30, 24 * 7, 24 * 2)  # 30d, 7d, 2d
    ]
    assert halves[0] < halves[1] < halves[2]


def test_quotes_on_grid_and_in_bounds():
    cfg = _cfg(tick_size="0.001")
    q = build_quotes(_book("0.37"), 24 * 30, 0.1, _flat(), cfg)
    assert Decimal(0) < q.bid < q.ask < Decimal(1)
    assert q.bid % cfg.tick_size == 0
    assert q.ask % cfg.tick_size == 0


def test_resolution_stop_returns_none():
    cfg = _cfg(min_hours_to_resolution="6")
    assert build_quotes(_book("0.50"), 3.0, 0.1, _flat(), cfg) is None  # 3h < 6h
    assert build_quotes(_book("0.50"), None, 0.1, _flat(), cfg) is None  # unknown


def test_gamma_stop_returns_none():
    # very close to resolution but resolution stop disabled -> gamma stop fires
    cfg = _cfg(min_hours_to_resolution="0", gamma_ceiling="0.5")
    assert build_quotes(_book("0.50"), 2.0, 1.0, _flat(), cfg) is None


def test_cadence_shorter_for_higher_vol():
    cfg = _cfg(min_cadence_seconds="60", max_cadence_seconds="1000000")
    mid, half = Decimal("0.50"), Decimal("0.02")
    calm = quote_cadence_seconds(mid, 1.0, half, cfg)
    fast = quote_cadence_seconds(mid, 10.0, half, cfg)
    assert fast < calm  # more drift -> requote sooner


def test_cadence_clamped_to_bounds():
    cfg = _cfg(min_cadence_seconds="300", max_cadence_seconds="172800")
    # huge vol -> tiny raw cadence -> floored at the minimum
    assert quote_cadence_seconds(Decimal("0.5"), 9999.0, Decimal("0.01"), cfg) == 300.0
    # near-zero vol -> enormous raw cadence -> capped at the maximum
    assert quote_cadence_seconds(Decimal("0.5"), 0.0001, Decimal("0.05"), cfg) == 172800.0


def test_single_sided_at_inventory_cap():
    cfg = _cfg(max_inventory="100", quote_size="10")
    capped_long = InventoryState("c1", net_yes=Decimal("100"))
    q = build_quotes(_book("0.50"), 24 * 30, 0.1, capped_long, cfg)
    assert q is not None
    assert q.is_two_sided is False
    assert q.bid_size == 0 and q.ask_size > 0  # cannot buy more YES; can still sell
