"""Live market making — places REAL resting CLOB orders. Double flag-guarded.

Mirrors the Phase 1 :class:`~common.execution.live.LiveExecutor` safety model:
real orders are placed only when **both** guards hold — ``config.mode == "live"``
AND ``i_understand_the_risks=True`` at construction. Disarmed, every method is a
no-op that places/cancels nothing.

Fills are not simulated: :meth:`reconcile` reads the account's trades from the
venue and matches them to our resting orders by id, settling inventory/PnL with
the same shared accounting as the paper executor. The ``book`` argument is part
of the :class:`MakerExecutor` contract but unused here (the venue is the source
of truth for fills).

Caps are enforced defensively: an order that could push ``|net_yes|`` past
``max_inventory`` is refused even when armed.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution.maker_base import MakerExecutor
from polymarket_bot.common.fees import FeeSchedule, rebate_rate, resolve_fee_rate
from polymarket_bot.common.models import Fill, InventoryState, MakerOrder, OrderBook, Side
from polymarket_bot.phase2_market_making.inventory import position_delta, settle_fill

logger = logging.getLogger(__name__)


class LiveMakerExecutor(MakerExecutor):
    """Places real resting orders via an injected CLOB client, behind a double guard."""

    def __init__(
        self,
        config: Config,
        clob_client,
        token_resolver,
        condition_id: str,
        category: str,
        fees: FeeSchedule,
        as_of: date,
        *,
        i_understand_the_risks: bool = False,
    ) -> None:
        self._config = config
        self._clob = clob_client
        self._resolve_tokens = token_resolver
        self._condition_id = condition_id
        self._category = category
        self._rate = resolve_fee_rate(category, fees, as_of)
        self._rebate_rate = rebate_rate(category)
        self._armed = bool(i_understand_the_risks)
        self._orders: dict[str, MakerOrder] = {}
        self._seen_trades: set[str] = set()
        self._inv = InventoryState(condition_id)

    @property
    def is_armed(self) -> bool:
        """True only when BOTH guards are satisfied."""
        return self._config.is_live and self._armed

    @property
    def open_orders(self) -> tuple[MakerOrder, ...]:
        return tuple(self._orders.values())

    @property
    def inventory(self) -> InventoryState:
        return self._inv

    @property
    def open_exposure(self) -> Decimal:
        return abs(self._inv.net_yes) * Decimal(1)

    def place(self, order: MakerOrder) -> MakerOrder:
        if not self.is_armed:
            logger.warning("LiveMakerExecutor refused to place: %s", self._disarmed_reason())
            return order  # unplaced: order_id stays None
        if self._would_breach_cap(order):
            logger.warning(
                "refusing order that could breach max_inventory=%s (net_yes=%s)",
                self._config.mm.max_inventory,
                self._inv.net_yes,
            )
            return order
        yes_token, no_token = self._resolve_tokens(order.condition_id)
        token_id = yes_token if order.side is Side.YES else no_token
        resp = self._clob.place_maker_order(order, token_id)
        order_id = _extract_order_id(resp)
        if order_id is None:
            logger.error("place_maker_order returned no order id: %r", resp)
            return order
        placed = replace(order, order_id=order_id)
        self._orders[order_id] = placed
        return placed

    def cancel(self, order_id: str) -> bool:
        if not self.is_armed or order_id not in self._orders:
            return False
        self._clob.cancel_order(order_id)
        del self._orders[order_id]
        return True

    def reconcile(self, book: OrderBook) -> tuple[Fill, ...]:  # noqa: ARG002 - venue is truth
        """Match the account's venue trades to resting orders and settle them."""
        if not self.is_armed:
            return ()
        fills: list[Fill] = []
        for trade in self._clob.get_trades() or []:
            trade_id = str(trade.get("id", ""))
            order_id = str(trade.get("order_id", trade.get("orderID", "")))
            if not trade_id or trade_id in self._seen_trades or order_id not in self._orders:
                continue
            self._seen_trades.add(trade_id)
            order = self._orders[order_id]
            size = Decimal(str(trade.get("size", "0")))
            if size <= 0:
                continue
            price = Decimal(str(trade.get("price", order.price)))
            fee = Decimal(str(trade.get("fee", "0")))
            fill = Fill(self._condition_id, order.side, price, size, fee)
            fills.append(fill)
            self._inv = settle_fill(
                self._inv, order, price=price, size=size, fee=fee,
                rebate_fraction=self._rebate_rate,
            )
            self._consume(order_id, order, size)
        return tuple(fills)

    def _consume(self, order_id: str, order: MakerOrder, filled: Decimal) -> None:
        """Drop a fully filled order; shrink a partial one."""
        remaining = order.size - filled
        if remaining <= 0:
            del self._orders[order_id]
        else:
            self._orders[order_id] = replace(order, size=remaining)

    def _would_breach_cap(self, order: MakerOrder) -> bool:
        delta = position_delta(order.side, buy=order.buy, size=order.size)
        return abs(self._inv.net_yes + delta) > self._config.mm.max_inventory

    def _disarmed_reason(self) -> str:
        if not self._config.is_live:
            return "config.mode is not 'live'"
        return "i_understand_the_risks flag not set"


def _extract_order_id(resp: object) -> str | None:
    if not isinstance(resp, dict):
        return None
    for key in ("orderID", "order_id", "id", "orderId"):
        if resp.get(key):
            return str(resp[key])
    return None
