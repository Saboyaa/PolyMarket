"""Tests for common.clients — Gamma (mocked httpx) and CLOB (mocked client).

No real network in the default run. One opt-in @pytest.mark.live test fetches a
real order book; it is excluded by the default ``-m 'not live'`` addopts.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from polymarket_bot.common.clients.clob import ClobMarketClient
from polymarket_bot.common.clients.gamma import GammaClient
from polymarket_bot.common.config import MarketSelector
from polymarket_bot.common.models import Order, Side

# --- Gamma fixtures ---------------------------------------------------------

MARKET_A = {
    "conditionId": "0xA",
    "question": "Will A happen?",
    "category": "Crypto",
    "clobTokenIds": json.dumps(["yesA", "noA"]),
    "active": True,
    "closed": False,
}
MARKET_B = {
    "conditionId": "0xB",
    "question": "Will B happen?",
    "category": "Sports",
    "clobTokenIds": ["yesB", "noB"],
    "active": True,
    "closed": False,
}
MARKET_NONBINARY = {
    "conditionId": "0xC",
    "question": "Three-way?",
    "category": "Politics",
    "clobTokenIds": json.dumps(["t1", "t2", "t3"]),
    "active": True,
}


def _gamma_with(markets: list[dict], capture: list | None = None) -> GammaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        offset = int(request.url.params.get("offset", "0"))
        page = markets if offset == 0 else []
        return httpx.Response(200, json=page)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://gamma.test")
    return GammaClient(client=client)


def test_discover_all_when_selector_empty():
    g = _gamma_with([MARKET_A, MARKET_B])
    markets = g.discover_markets(MarketSelector())
    assert {m.condition_id for m in markets} == {"0xA", "0xB"}
    assert markets[0].yes_token_id == "yesA"
    assert markets[1].yes_token_id == "yesB"


def test_discover_skips_non_binary():
    g = _gamma_with([MARKET_A, MARKET_NONBINARY])
    markets = g.discover_markets()
    assert {m.condition_id for m in markets} == {"0xA"}


def test_selector_condition_id_allowlist():
    g = _gamma_with([MARKET_A, MARKET_B])
    sel = MarketSelector(condition_ids=["0xB"])
    markets = g.discover_markets(sel)
    assert [m.condition_id for m in markets] == ["0xB"]


def test_selector_category_filter():
    g = _gamma_with([MARKET_A, MARKET_B])
    sel = MarketSelector(categories=["Crypto"])
    markets = g.discover_markets(sel)
    assert [m.condition_id for m in markets] == ["0xA"]


def test_gamma_stops_on_422_offset_overflow():
    """A full catalog deeper than the offset cap must not crash (regression).

    Gamma returns HTTP 422 once ``offset`` runs past its ceiling; pagination
    should treat that as end-of-results, not raise.
    """
    full_page = [dict(MARKET_A, conditionId=f"0x{i}") for i in range(100)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json=full_page)  # full -> client pages on
        return httpx.Response(422, json={"error": "offset out of range"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gamma.test")
    g = GammaClient(client=client)
    markets = g.discover_markets()
    assert len(markets) == 100  # first page kept; 422 stopped pagination cleanly


def test_gamma_does_not_page_past_offset_ceiling():
    """Pagination must stop at the offset cap rather than request beyond it."""
    full_page = [dict(MARKET_A, conditionId=f"0x{i}") for i in range(100)]
    seen_offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_offsets.append(int(request.url.params.get("offset", "0")))
        return httpx.Response(200, json=full_page)  # always full -> would loop forever

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://gamma.test")
    g = GammaClient(client=client)
    g.discover_markets()
    assert max(seen_offsets) <= 10_000  # never requests an out-of-range offset


def test_gamma_queries_active_nonclosed():
    captured: list[httpx.Request] = []
    g = _gamma_with([MARKET_A], capture=captured)
    g.discover_markets()
    params = captured[0].url.params
    assert params.get("active") == "true"
    assert params.get("closed") == "false"


# --- CLOB order book --------------------------------------------------------


def _book(asks, bids):
    return SimpleNamespace(
        asks=[SimpleNamespace(price=p, size=s) for p, s in asks],
        bids=[SimpleNamespace(price=p, size=s) for p, s in bids],
    )


class FakeClob:
    def __init__(self):
        self.books = {}
        self.orders = []
        self.created = []

    def get_order_book(self, token_id):
        return self.books[token_id]

    def create_order(self, args):
        self.created.append(args)
        return {"signed": True, "token": args.token_id}

    def post_order(self, signed, order_type):
        self.orders.append((signed, order_type))
        return {"status": "matched", "size": "10", "price": "0.40"}


def test_get_order_book_returns_decimal_levels_sorted():
    fc = FakeClob()
    fc.books["yes"] = _book(asks=[("0.42", "50"), ("0.40", "100")], bids=[("0.39", "30")])
    fc.books["no"] = _book(asks=[("0.55", "80")], bids=[("0.50", "20")])
    client = ClobMarketClient(fc)

    book = client.get_order_book("cond1", "yes", "no")
    assert book.condition_id == "cond1"
    # asks ascending by price
    assert [lv.price for lv in book.yes_asks] == [Decimal("0.40"), Decimal("0.42")]
    assert isinstance(book.yes_asks[0].size, Decimal)
    assert book.no_asks[0].price == Decimal("0.55")
    # bids descending
    assert book.yes_bids[0].price == Decimal("0.39")


def test_get_order_book_drops_zero_size_levels():
    fc = FakeClob()
    fc.books["yes"] = _book(asks=[("0.40", "0"), ("0.41", "5")], bids=[])
    fc.books["no"] = _book(asks=[("0.55", "10")], bids=[])
    client = ClobMarketClient(fc)
    book = client.get_order_book("c", "yes", "no")
    assert [lv.price for lv in book.yes_asks] == [Decimal("0.41")]


def test_place_order_creates_and_posts_buy():
    fc = FakeClob()
    client = ClobMarketClient(fc)
    order = Order(condition_id="c", side=Side.YES, price=Decimal("0.40"), size=Decimal("10"))
    resp = client.place_order(order, "yes_token")
    assert resp["status"] == "matched"
    assert fc.created[0].token_id == "yes_token"
    assert fc.created[0].side == "BUY"
    assert fc.created[0].price == 0.40


def test_clob_backoff_retries_on_rate_limit(monkeypatch):
    from py_clob_client.exceptions import PolyApiException

    calls = {"n": 0}

    class RateLimitedThenOk:
        def get_order_book(self, token_id):
            return _book([("0.4", "1")], [])

        def create_order(self, args):
            return {}

        def post_order(self, signed, order_type):
            calls["n"] += 1
            if calls["n"] == 1:
                exc = PolyApiException(error_msg="rate")
                exc.status_code = 429
                raise exc
            return {"status": "matched", "size": "1", "price": "0.4"}

    monkeypatch.setattr("time.sleep", lambda *_: None)
    client = ClobMarketClient(RateLimitedThenOk(), max_retries=3)
    order = Order(condition_id="c", side=Side.NO, price=Decimal("0.4"), size=Decimal("1"))
    resp = client.place_order(order, "no_token")
    assert resp["status"] == "matched"
    assert calls["n"] == 2


# --- opt-in real read-only test (excluded by default) -----------------------


@pytest.mark.live
def test_live_real_order_book_read_only():  # pragma: no cover - manual opt-in
    """Fetch one real order book. Run with: pytest -m live"""
    from py_clob_client.client import ClobClient

    from polymarket_bot.common.auth import CHAIN_ID, CLOB_HOST

    raw_client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID)
    # A well-known liquid token id can be set via env for manual runs.
    import os

    token = os.environ.get("POLYMARKET_LIVE_TOKEN_ID")
    if not token:
        pytest.skip("set POLYMARKET_LIVE_TOKEN_ID to run the live book fetch")
    book = raw_client.get_order_book(token)
    assert book is not None
