"""Internal value objects shared across phases.

Money is always ``Decimal`` — never float — to avoid rounding drift in edge/fee math.
Prices are in USDC per share in the range [0, 1]; sizes are share counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    YES = "YES"
    NO = "NO"


@dataclass(frozen=True)
class Market:
    """A binary (YES/NO) Polymarket market.

    ``end_date`` is the market's resolution / end time (UTC), when known. Phase 2
    market making needs it to compute time-to-resolution ``T``; it is ``None`` for
    markets where Gamma does not report a date.
    """

    condition_id: str
    question: str
    category: str
    yes_token_id: str
    no_token_id: str
    active: bool = True
    end_date: datetime | None = None


@dataclass(frozen=True)
class Level:
    """A single price level in an order book."""

    price: Decimal  # USDC per share, 0..1
    size: Decimal  # shares available at this price

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.price <= Decimal(1)):
            raise ValueError(f"price must be in [0, 1], got {self.price}")
        if self.size < 0:
            raise ValueError(f"size must be >= 0, got {self.size}")


@dataclass(frozen=True)
class OrderBook:
    """Order book for one market.

    ``yes_asks`` / ``no_asks`` are the levels we would *buy* into, best (lowest)
    price first. Bids are included for completeness / Phase 2.
    """

    condition_id: str
    yes_asks: tuple[Level, ...] = field(default_factory=tuple)
    no_asks: tuple[Level, ...] = field(default_factory=tuple)
    yes_bids: tuple[Level, ...] = field(default_factory=tuple)
    no_bids: tuple[Level, ...] = field(default_factory=tuple)

    def best_ask(self, side: Side) -> Level | None:
        asks = self.yes_asks if side is Side.YES else self.no_asks
        return asks[0] if asks else None

    def best_bid(self, side: Side) -> Level | None:
        """Highest price someone is bidding (we would *sell* into), best first."""
        bids = self.yes_bids if side is Side.YES else self.no_bids
        return bids[0] if bids else None

    def mid_price(self, side: Side) -> Decimal | None:
        """Midpoint of best bid and best ask for ``side``, or ``None`` if a side
        is missing. This is the seed fair value for market making."""
        bid, ask = self.best_bid(side), self.best_ask(side)
        if bid is None or ask is None:
            return None
        return (bid.price + ask.price) / Decimal(2)


@dataclass(frozen=True)
class Opportunity:
    """A detected intra-market arbitrage opportunity (already sized)."""

    condition_id: str
    category: str
    yes_ask: Decimal  # price to buy YES
    no_ask: Decimal  # price to buy NO
    size: Decimal  # shares to buy on each side
    gross_edge_per_share: Decimal  # 1 - (yes_ask + no_ask)
    net_edge_per_share: Decimal  # gross minus per-share fees; must be > 0 to act

    @property
    def notional(self) -> Decimal:
        """USDC outlay to open both legs."""
        return self.size * (self.yes_ask + self.no_ask)

    @property
    def expected_profit(self) -> Decimal:
        return self.size * self.net_edge_per_share


@dataclass(frozen=True)
class Order:
    """An intent to buy ``size`` shares of ``side`` at up to ``price``."""

    condition_id: str
    side: Side
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class Fill:
    """The result of (attempting to) execute an order."""

    condition_id: str
    side: Side
    price: Decimal  # average fill price
    size: Decimal  # shares actually filled
    fee: Decimal  # USDC fee paid on this fill


# --- Phase 2: market making -------------------------------------------------


@dataclass(frozen=True)
class Quote:
    """A two-sided quote on the YES token for one market.

    We quote the YES book: a ``bid`` we are willing to buy YES at and an ``ask``
    we are willing to sell YES at, ``size`` shares each side. The NO side is the
    mirror (``no_bid = 1 − ask``, ``no_ask = 1 − bid``), since ``NO = 1 − YES``.
    """

    condition_id: str
    bid: Decimal  # YES buy price
    ask: Decimal  # YES sell price
    size: Decimal  # shares quoted per side

    def __post_init__(self) -> None:
        for name, px in (("bid", self.bid), ("ask", self.ask)):
            if not (Decimal(0) < px < Decimal(1)):
                raise ValueError(f"{name} must be in (0, 1), got {px}")
        if self.bid >= self.ask:
            raise ValueError(f"quote must not cross: bid {self.bid} >= ask {self.ask}")
        if self.size <= 0:
            raise ValueError(f"size must be > 0, got {self.size}")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def half_spread(self) -> Decimal:
        return (self.ask - self.bid) / Decimal(2)


@dataclass(frozen=True)
class MakerOrder:
    """A single resting limit order. ``buy`` distinguishes a bid from an ask.

    ``order_id`` is assigned by the venue after placement; ``None`` before.
    """

    condition_id: str
    side: Side  # which token (YES/NO) the order rests on
    buy: bool  # True = bid (buy), False = ask (sell)
    price: Decimal
    size: Decimal
    order_id: str | None = None

    def __post_init__(self) -> None:
        if not (Decimal(0) < self.price < Decimal(1)):
            raise ValueError(f"price must be in (0, 1), got {self.price}")
        if self.size <= 0:
            raise ValueError(f"size must be > 0, got {self.size}")


@dataclass(frozen=True)
class InventoryState:
    """Signed inventory and running PnL for one market.

    ``net_yes`` is signed shares of YES (negative = net long NO). Updates are
    pure: ``common.execution`` / ``phase2.inventory`` produce new instances.
    """

    condition_id: str
    net_yes: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    rebates_earned: Decimal = Decimal(0)

    @property
    def is_flat(self) -> bool:
        return self.net_yes == 0

    @property
    def net_pnl(self) -> Decimal:
        """Realized PnL net of fees and inclusive of rebates earned."""
        return self.realized_pnl - self.fees_paid + self.rebates_earned
