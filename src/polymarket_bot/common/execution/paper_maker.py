"""Deterministic paper market making — resting orders, simulated fills.

:class:`PaperMakerExecutor` is long-lived and book-agnostic at construction: you
``place`` resting orders, then ``reconcile`` against each scan's book. A resting
*bid* fills when a seller crosses to it (book best ask ≤ our price); a resting
*ask* fills when a buyer crosses up (book best bid ≥ our price). The maker gets
*its own* price on the fill.

Each fill pays the standard per-share fee and earns a maker rebate, modeled as
``rebate_rate(category) × fee`` (a simplification of Polymarket's pool-based
program — see ``docs/spec-phase2.md``). Inventory, fees, rebates, and cash flow
accumulate on the :class:`InventoryState`; ``realized_pnl`` is signed cash flow,
so when the position returns flat it equals the spread captured (the basis for
the PnL invariant: flat round-trip PnL == spread − fees + rebates).

All money is :class:`Decimal`; no network — fully deterministic.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from polymarket_bot.common.execution.maker_base import MakerExecutor
from polymarket_bot.common.fees import FeeSchedule, leg_fee, rebate_rate, resolve_fee_rate
from polymarket_bot.common.models import Fill, InventoryState, MakerOrder, OrderBook
from polymarket_bot.phase2_market_making.inventory import settle_fill


class PaperMakerExecutor(MakerExecutor):
    """Simulates resting-order fills against successive book snapshots."""

    def __init__(
        self,
        condition_id: str,
        category: str,
        fees: FeeSchedule,
        as_of: date,
    ) -> None:
        self._condition_id = condition_id
        self._category = category
        self._fees = fees
        self._rate = resolve_fee_rate(category, fees, as_of)
        self._rebate_rate = rebate_rate(category)
        self._orders: dict[str, MakerOrder] = {}
        self._next_id = 0
        self._inv = InventoryState(condition_id)

    @property
    def open_orders(self) -> tuple[MakerOrder, ...]:
        return tuple(self._orders.values())

    @property
    def inventory(self) -> InventoryState:
        return self._inv

    @property
    def open_exposure(self) -> Decimal:
        # Conservative: each net share can lose at most its full $1 payout.
        return abs(self._inv.net_yes) * Decimal(1)

    def place(self, order: MakerOrder) -> MakerOrder:
        self._next_id += 1
        order_id = f"paper-{self._next_id}"
        placed = replace(order, order_id=order_id)
        self._orders[order_id] = placed
        return placed

    def cancel(self, order_id: str) -> bool:
        return self._orders.pop(order_id, None) is not None

    def reconcile(self, book: OrderBook) -> tuple[Fill, ...]:
        fills: list[Fill] = []
        for order_id, order in list(self._orders.items()):
            fill = self._maybe_fill(order, book)
            if fill is None:
                continue
            fills.append(fill)
            del self._orders[order_id]
            self._settle(order, fill)
        return tuple(fills)

    def _maybe_fill(self, order: MakerOrder, book: OrderBook) -> Fill | None:
        """A resting order fills when the book trades through its price."""
        if order.buy:  # our bid fills if a seller crosses down to us
            counter = book.best_ask(order.side)
            crosses = counter is not None and counter.price <= order.price
        else:  # our ask fills if a buyer crosses up to us
            counter = book.best_bid(order.side)
            crosses = counter is not None and counter.price >= order.price
        if not crosses or counter.size <= 0:
            return None
        size = min(order.size, counter.size)
        fee = leg_fee(size, self._rate, order.price)
        return Fill(
            condition_id=self._condition_id,
            side=order.side,
            price=order.price,  # maker fills at its own resting price
            size=size,
            fee=fee,
        )

    def _settle(self, order: MakerOrder, fill: Fill) -> None:
        self._inv = settle_fill(
            self._inv,
            order,
            price=fill.price,
            size=fill.size,
            fee=fill.fee,
            rebate_fraction=self._rebate_rate,
        )
