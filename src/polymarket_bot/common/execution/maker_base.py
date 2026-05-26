"""The maker-execution contract shared by paper and live market making.

Unlike the Phase 1 taker :class:`Executor` (which fires a sized
:class:`Opportunity` and returns immediately), a market maker rests limit orders
on the book and lets them fill over time. A :class:`MakerExecutor` is therefore
long-lived: it ``place``s and ``cancel``s resting orders and is ``reconcile``d
against each fresh book snapshot to discover fills, accumulating signed inventory
and PnL (net of fees, inclusive of maker rebates) as it goes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from polymarket_bot.common.models import Fill, InventoryState, MakerOrder, OrderBook


class MakerExecutor(ABC):
    """Manages resting maker orders for one market. Paper or live."""

    @abstractmethod
    def place(self, order: MakerOrder) -> MakerOrder:
        """Rest ``order`` on the book; return it with an assigned ``order_id``."""

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """Cancel a resting order; return whether it was found and removed."""

    @abstractmethod
    def reconcile(self, book: OrderBook) -> tuple[Fill, ...]:
        """Settle resting orders against ``book``; return any fills and update state."""

    @property
    @abstractmethod
    def open_orders(self) -> tuple[MakerOrder, ...]:
        """Currently resting orders."""

    @property
    @abstractmethod
    def inventory(self) -> InventoryState:
        """Signed inventory and running PnL for this market."""

    @property
    @abstractmethod
    def open_exposure(self) -> Decimal:
        """Conservative USDC exposure of the open position (read by risk caps)."""
