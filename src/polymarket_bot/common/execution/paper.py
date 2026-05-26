"""Deterministic paper (simulated) execution.

:class:`PaperExecutor` fills an :class:`Opportunity` against a *provided* order
book — no network, fully deterministic — so paper mode doubles as a test
harness. It buys the YES leg first, then sizes the NO leg to match the YES
shares actually filled (staying hedged), and tracks cumulative open exposure
and realized P&L.

If the NO best ask has moved above the opportunity's expected ``no_ask`` (leg 2
moved), the executor crosses the spread to complete the pair at the current
best ask — but only while the extra cost per share stays within
``max_completion_slippage``. If completion would exceed that bound, the NO leg
is *not* taken and the result is marked not completed (the caller must handle
the unhedged YES leg, mirroring the live halt path).

All money is :class:`Decimal`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from polymarket_bot.common.execution.base import ExecutionResult, Executor
from polymarket_bot.common.fees import FeeSchedule, leg_fee, resolve_fee_rate
from polymarket_bot.common.models import Fill, Level, Opportunity, OrderBook, Side


class PaperExecutor(Executor):
    """Simulates fills from a fixed book; tracks exposure and realized P&L."""

    def __init__(
        self,
        book: OrderBook,
        fees: FeeSchedule,
        max_completion_slippage: Decimal,
        as_of: date,
    ) -> None:
        self._book = book
        self._fees = fees
        self._max_completion_slippage = max_completion_slippage
        self._as_of = as_of
        self._open_exposure = Decimal(0)
        self._realized_pnl = Decimal(0)

    @property
    def open_exposure(self) -> Decimal:
        return self._open_exposure

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    def _fill_at_best(self, side: Side, want: Decimal) -> Level | None:
        """Return the best-ask level for ``side`` if it has any size, else None."""
        level = self._book.best_ask(side)
        if level is None or level.size <= 0 or want <= 0:
            return None
        return level

    def execute(self, opportunity: Opportunity) -> ExecutionResult:
        rate = resolve_fee_rate(opportunity.category, self._fees, self._as_of)

        # --- Leg 1: YES ---
        yes_level = self._fill_at_best(Side.YES, opportunity.size)
        if yes_level is None:
            return ExecutionResult(
                opportunity=opportunity,
                fills=(),
                completed=False,
                realized_edge=Decimal(0),
                note="no YES liquidity; nothing executed",
            )

        yes_qty = min(opportunity.size, yes_level.size)
        yes_price = yes_level.price
        yes_fee = leg_fee(yes_qty, rate, yes_price)
        yes_fill = Fill(
            condition_id=opportunity.condition_id,
            side=Side.YES,
            price=yes_price,
            size=yes_qty,
            fee=yes_fee,
        )

        # --- Leg 2: NO, sized to match the YES shares filled (stay hedged) ---
        no_level = self._fill_at_best(Side.NO, yes_qty)
        if no_level is None:
            # Cannot hedge at all: leave the YES leg unhedged, not completed.
            self._open_exposure += yes_qty * yes_price
            return ExecutionResult(
                opportunity=opportunity,
                fills=(yes_fill,),
                completed=False,
                realized_edge=Decimal(0),
                note="no NO liquidity to hedge; YES leg left open",
            )

        no_price = no_level.price
        slippage = no_price - opportunity.no_ask
        note = ""
        if slippage > self._max_completion_slippage:
            # Crossing the spread would exceed the slippage bound: do not take
            # the hedge (mirrors the live alert+halt path).
            self._open_exposure += yes_qty * yes_price
            return ExecutionResult(
                opportunity=opportunity,
                fills=(yes_fill,),
                completed=False,
                realized_edge=Decimal(0),
                note=(
                    f"NO leg moved to {no_price} (expected {opportunity.no_ask}); "
                    f"slippage {slippage} exceeds cap {self._max_completion_slippage}; "
                    "YES leg left open"
                ),
            )
        if slippage > 0:
            note = (
                f"completed by crossing the spread: NO filled at {no_price} "
                f"(expected {opportunity.no_ask}, slippage {slippage})"
            )

        no_qty = min(yes_qty, no_level.size)
        no_fee = leg_fee(no_qty, rate, no_price)
        no_fill = Fill(
            condition_id=opportunity.condition_id,
            side=Side.NO,
            price=no_price,
            size=no_qty,
            fee=no_fee,
        )

        # Paired size is the lesser of the two legs (fully hedged portion).
        # Both legs are guaranteed positive here, so paired > 0.
        paired = min(yes_qty, no_qty)
        cost = paired * (yes_price + no_price)
        total_fee = yes_fee + no_fee
        # Realized edge per (paired) share: $1 payout minus cost minus fees.
        realized_edge = (paired * Decimal(1) - cost - total_fee) / paired

        self._open_exposure += cost
        self._realized_pnl += paired * Decimal(1) - cost - total_fee

        completed = yes_qty == no_qty and no_qty > 0

        return ExecutionResult(
            opportunity=opportunity,
            fills=(yes_fill, no_fill),
            completed=completed,
            realized_edge=realized_edge,
            note=note,
        )
