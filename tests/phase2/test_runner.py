from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polymarket_bot.common.config import Config, MarketMakingConfig, RiskConfig
from polymarket_bot.common.execution import PaperMakerExecutor
from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import Level, Market, OrderBook
from polymarket_bot.phase2_market_making.runner import MMRunner

_NOW = datetime(2026, 5, 26, tzinfo=UTC)


def _market(cid="c1", *, active=True, days_to_end=30) -> Market:
    return Market(
        condition_id=cid,
        question=f"Q-{cid}?",
        category="Politics",
        yes_token_id=f"{cid}-y",
        no_token_id=f"{cid}-n",
        active=active,
        end_date=_NOW + timedelta(days=days_to_end),
    )


def _book(mid="0.50", spread="0.04", cid="c1") -> OrderBook:
    m, s = Decimal(mid), Decimal(spread)
    return OrderBook(
        condition_id=cid,
        yes_asks=(Level(m + s / 2, Decimal("100")),),
        yes_bids=(Level(m - s / 2, Decimal("100")),),
    )


class FakeMarketSource:
    def __init__(self, markets):
        self._markets = markets

    def discover_markets(self, _selector):
        return list(self._markets)


class FakeBookSource:
    def __init__(self, books):
        self.books = books  # cid -> OrderBook (mutable) or Exception

    def get_order_book(self, cid, _y, _n):
        b = self.books[cid]
        if isinstance(b, Exception):
            raise b
        return b


def _cfg(**mm):
    base = dict(base_spread="0.01", k_gamma="0.05", sigma="0.1", tick_size="0.01",
                min_hours_to_resolution="6", gamma_ceiling="100", quote_size="10")
    base.update(mm)
    return Config(mm=MarketMakingConfig(**base), risk=RiskConfig())


def _runner(markets, books, cfg=None, **kw):
    cfg = cfg or _cfg()

    def factory(market):
        return PaperMakerExecutor(market.condition_id, market.category,
                                  FeeSchedule.default(), _NOW.date())

    return MMRunner(cfg, FakeMarketSource(markets), FakeBookSource(books), factory,
                    clock=lambda: _NOW, sleep=lambda _s: None, **kw)


def test_places_two_sided_quote():
    r = _runner([_market()], {"c1": _book()})
    r.scan_once()
    ex = r._executors["c1"]
    orders = ex.open_orders
    assert len(orders) == 2
    assert {o.buy for o in orders} == {True, False}  # a bid and an ask
    assert r.stats["quoted"] == 1


def test_resolution_stop_places_nothing():
    r = _runner([_market(days_to_end=0)], {"c1": _book()})  # ~0h to resolution
    r.scan_once()
    assert r._executors["c1"].open_orders == ()
    assert r.stats["stopped"] == 1


def test_fill_updates_inventory_and_count():
    books = FakeBookSource({"c1": _book()})
    r = _runner([_market()], books.books)
    r.scan_once()  # places bid ~0.49 and ask ~0.51
    # advance past the market's requote cadence so it is processed again
    r._clock = lambda: _NOW + timedelta(days=10)
    # next scan: ask collapses so the resting bid fills
    books.books["c1"] = _book(mid="0.40")
    r._books = books
    r.scan_once()
    ex = r._executors["c1"]
    assert r.stats["fills"] >= 1
    assert ex.inventory.net_yes > 0  # bought YES


def test_no_mid_skips_quoting():
    book = OrderBook(condition_id="c1", yes_asks=(Level(Decimal("0.5"), Decimal("10")),))
    r = _runner([_market()], {"c1": book})
    r.scan_once()
    assert r._executors["c1"].open_orders == ()


def test_exposure_cap_blocks_new_orders():
    cfg = _cfg()
    cfg.risk.max_total_exposure = Decimal("5")
    books = FakeBookSource({"c1": _book()})
    r = _runner([_market()], books.books, cfg=cfg)
    r.scan_once()
    r._clock = lambda: _NOW + timedelta(days=10)  # past the requote cadence
    books.books["c1"] = _book(mid="0.40")  # fills the resting bid (10 shares)
    r._books = books
    r.scan_once()
    ex = r._executors["c1"]
    assert ex.inventory.net_yes >= 10  # got filled
    assert r.total_exposure >= Decimal("5")  # over the cap now
    assert ex.open_orders == ()  # capped -> placed nothing new


def test_book_error_does_not_kill_loop():
    r = _runner(
        [_market("c1"), _market("c2")],
        {"c1": RuntimeError("down"), "c2": _book(cid="c2")},
    )
    r.scan_once()  # must not raise
    assert "c2" in r._executors and len(r._executors["c2"].open_orders) == 2


def test_inactive_market_skipped():
    r = _runner([_market("c1", active=False)], {"c1": _book()})
    r.scan_once()
    assert r._executors == {}


def test_run_bounded_by_max_scans():
    r = _runner([_market()], {"c1": _book()})
    r.run(max_scans=3)
    assert r.stats["scans"] == 3


def test_market_not_requoted_until_cadence_elapses():
    r = _runner([_market()], {"c1": _book()})
    r.scan_once()  # processed: quote placed, next-due scheduled in the future
    assert r.stats["quoted"] == 1
    r.scan_once()  # same clock -> not yet due -> skipped
    assert r.stats["quoted"] == 1
    assert r.stats["skipped"] == 1
    # advance past the cadence -> processed again
    r._clock = lambda: _NOW + timedelta(days=10)
    r.scan_once()
    assert r.stats["quoted"] == 2
