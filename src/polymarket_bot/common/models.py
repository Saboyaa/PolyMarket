"""Internal value objects shared across phases.

Money is always ``Decimal`` — never float — to avoid rounding drift in edge/fee math.
Prices are in USDC per share in the range [0, 1]; sizes are share counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    YES = "YES"
    NO = "NO"


@dataclass(frozen=True)
class Market:
    """A binary (YES/NO) Polymarket market."""

    condition_id: str
    question: str
    category: str
    yes_token_id: str
    no_token_id: str
    active: bool = True


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
