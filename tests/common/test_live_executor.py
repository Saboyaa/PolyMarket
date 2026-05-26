"""Tests for LiveExecutor — MOCKED clob client, never places real orders.

Covers: double-guard refusal, happy-path completion, cross-the-spread completion
within slippage, and the halt path when completion is impossible.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution.live import LiveExecutor
from polymarket_bot.common.models import Level, Opportunity, OrderBook, Side

TOKENS = {"c1": ("yes_tok", "no_tok")}


def _resolver(cond_id: str) -> tuple[str, str]:
    return TOKENS[cond_id]


def _live_config(**risk) -> Config:
    data = {"mode": "live"}
    if risk:
        data["risk"] = risk
    return Config.model_validate(data)


def _opp(size="10", yes="0.40", no="0.55") -> Opportunity:
    return Opportunity(
        condition_id="c1",
        category="Crypto",
        yes_ask=Decimal(yes),
        no_ask=Decimal(no),
        size=Decimal(size),
        gross_edge_per_share=Decimal("0.05"),
        net_edge_per_share=Decimal("0.02"),
    )


class MockClob:
    """Records placed orders; returns canned fills keyed by call order."""

    def __init__(self, fills, book=None):
        self._fills = list(fills)
        self._book = book
        self.placed: list = []
        self.book_fetches = 0

    def place_order(self, order, token_id):
        self.placed.append((order, token_id))
        resp = self._fills.pop(0)
        return resp

    def get_order_book(self, condition_id, yes_token, no_token):
        self.book_fetches += 1
        return self._book


# --- guard refusal ----------------------------------------------------------


def test_refuses_when_not_live_mode():
    clob = MockClob(fills=[])
    ex = LiveExecutor(
        Config.model_validate({"mode": "paper"}),
        clob,
        _resolver,
        i_understand_the_risks=True,
    )
    res = ex.execute(_opp())
    assert res.completed is False
    assert res.fills == ()
    assert "not 'live'" in res.note
    assert clob.placed == []
    assert ex.open_exposure == Decimal(0)


def test_refuses_without_risk_flag():
    clob = MockClob(fills=[])
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=False)
    res = ex.execute(_opp())
    assert res.completed is False
    assert "risks" in res.note
    assert clob.placed == []


def test_is_armed_requires_both():
    assert (
        LiveExecutor(_live_config(), MockClob([]), _resolver, i_understand_the_risks=True).is_armed
        is True
    )
    assert (
        LiveExecutor(_live_config(), MockClob([]), _resolver, i_understand_the_risks=False).is_armed
        is False
    )


# --- happy path -------------------------------------------------------------


def test_both_legs_fill_completes():
    clob = MockClob(
        fills=[
            {"status": "matched", "size": "10", "price": "0.40", "fee": "0.01"},
            {"status": "matched", "size": "10", "price": "0.55", "fee": "0.01"},
        ]
    )
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.completed is True
    assert len(res.fills) == 2
    assert res.fills[0].side is Side.YES
    assert res.fills[1].side is Side.NO
    assert ex.open_exposure > 0
    # no completion book fetch needed
    assert clob.book_fetches == 0


def test_leg1_no_fill_takes_no_exposure():
    clob = MockClob(fills=[{"status": "unmatched"}])
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.completed is False
    assert res.fills == ()
    assert "leg 1" in res.note
    assert len(clob.placed) == 1  # only attempted leg 1


# --- cross-the-spread completion --------------------------------------------


def test_completion_within_slippage():
    # leg 2 unmatched; completion book has NO ask at 0.56 (<= 0.55 + 0.02).
    book = OrderBook(
        condition_id="c1",
        no_asks=(Level(Decimal("0.56"), Decimal("10")),),
    )
    clob = MockClob(
        fills=[
            {"status": "matched", "size": "10", "price": "0.40", "fee": "0"},
            {"status": "unmatched"},
            {"status": "matched", "size": "10", "price": "0.56", "fee": "0"},
        ],
        book=book,
    )
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.completed is True
    assert clob.book_fetches == 1
    # completion order placed at the crossed price
    completion_order = clob.placed[-1][0]
    assert completion_order.price == Decimal("0.56")
    assert completion_order.side is Side.NO
    assert "crossing spread" in res.note


def test_partial_leg2_then_completion():
    # leg2 fills 6, remaining 4 completed by crossing.
    book = OrderBook(
        condition_id="c1",
        no_asks=(Level(Decimal("0.56"), Decimal("100")),),
    )
    clob = MockClob(
        fills=[
            {"status": "matched", "size": "10", "price": "0.40", "fee": "0"},
            {"status": "matched", "size": "6", "price": "0.55", "fee": "0"},
            {"status": "matched", "size": "4", "price": "0.56", "fee": "0"},
        ],
        book=book,
    )
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.completed is True
    assert clob.placed[-1][0].size == Decimal("4")


# --- halt path --------------------------------------------------------------


def test_halt_when_no_depth_within_slippage():
    # only ask is at 0.60 which exceeds 0.55 + 0.02 = 0.57.
    book = OrderBook(
        condition_id="c1",
        no_asks=(Level(Decimal("0.60"), Decimal("100")),),
    )
    clob = MockClob(
        fills=[
            {"status": "matched", "size": "10", "price": "0.40", "fee": "0"},
            {"status": "unmatched"},
        ],
        book=book,
    )
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.completed is False
    assert "HALT" in res.note
    # no completion order placed beyond the two attempts
    assert len(clob.placed) == 2


def test_halt_when_insufficient_depth():
    # ask within slippage but not enough size to hedge 10 shares.
    book = OrderBook(
        condition_id="c1",
        no_asks=(Level(Decimal("0.56"), Decimal("3")),),
    )
    clob = MockClob(
        fills=[
            {"status": "matched", "size": "10", "price": "0.40", "fee": "0"},
            {"status": "unmatched"},
        ],
        book=book,
    )
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.completed is False
    assert "HALT" in res.note


def test_fee_fn_used_when_response_lacks_fee():
    def fee_fn(category, price, shares):
        return Decimal("0.005") * shares

    clob = MockClob(
        fills=[
            {"status": "matched", "size": "10", "price": "0.40"},
            {"status": "matched", "size": "10", "price": "0.55"},
        ]
    )
    ex = LiveExecutor(_live_config(), clob, _resolver, fee_fn=fee_fn, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.fills[0].fee == Decimal("0.05")


def test_completion_slippage_boundary_inclusive():
    # ask exactly at 0.55 + 0.02 = 0.57 should be allowed.
    book = OrderBook(
        condition_id="c1",
        no_asks=(Level(Decimal("0.57"), Decimal("10")),),
    )
    clob = MockClob(
        fills=[
            {"status": "matched", "size": "10", "price": "0.40", "fee": "0"},
            {"status": "unmatched"},
            {"status": "matched", "size": "10", "price": "0.57", "fee": "0"},
        ],
        book=book,
    )
    ex = LiveExecutor(_live_config(), clob, _resolver, i_understand_the_risks=True)
    res = ex.execute(_opp())
    assert res.completed is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
