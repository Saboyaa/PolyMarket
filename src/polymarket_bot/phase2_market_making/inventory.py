"""Inventory tracking, skew, and the hard risk stops (pure functions).

Inventory is signed YES shares (``net_yes`` on :class:`InventoryState`); negative
means net long NO. These helpers update the position from fills, compute the
reservation-price skew that mean-reverts inventory toward target, size the two
quote sides so the cap can never be breached, and evaluate the resolution and
gamma stops. No I/O — :mod:`phase2.strategy` and the runner compose them.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from polymarket_bot.common.config import MarketMakingConfig
from polymarket_bot.common.models import InventoryState, MakerOrder, Side

_REBATE_QUANTUM = Decimal("0.00001")


def position_delta(side: Side, *, buy: bool, size: Decimal) -> Decimal:
    """Signed change in net YES from a fill.

    Buying YES (+) and selling NO (+) grow the long-YES position; selling YES (−)
    and buying NO (−) shrink it, since ``NO = 1 − YES``.
    """
    sign = Decimal(1) if buy else Decimal(-1)
    if side is Side.NO:
        sign = -sign
    return sign * size


def apply_fill(
    inv: InventoryState, side: Side, *, buy: bool, size: Decimal, fee: Decimal
) -> InventoryState:
    """Return a new state with the fill's signed size and fee applied.

    Realized PnL and rebates are credited by the executor (which knows the quote
    context); this tracks position and fees paid.
    """
    return replace(
        inv,
        net_yes=inv.net_yes + position_delta(side, buy=buy, size=size),
        fees_paid=inv.fees_paid + fee,
    )


def settle_fill(
    inv: InventoryState,
    order: MakerOrder,
    *,
    price: Decimal,
    size: Decimal,
    fee: Decimal,
    rebate_fraction: Decimal,
) -> InventoryState:
    """Apply a maker fill: position, fee, signed cash flow, and maker rebate.

    Shared by the paper and live maker executors so their PnL accounting is
    identical. ``realized_pnl`` is signed cash flow (buy pays out, sell takes in),
    so a flat round-trip equals the spread captured.
    """
    inv = apply_fill(inv, order.side, buy=order.buy, size=size, fee=fee)
    cash = price * size
    cash_flow = -cash if order.buy else cash
    rebate = (rebate_fraction * fee).quantize(_REBATE_QUANTUM)
    return replace(
        inv,
        realized_pnl=inv.realized_pnl + cash_flow,
        rebates_earned=inv.rebates_earned + rebate,
    )


def skew(net_yes: Decimal, config: MarketMakingConfig) -> Decimal:
    """Reservation-price offset ``k_inv * (net_yes − target)``.

    Positive when long YES (subtract from mid → quote lower → encourage selling),
    negative when short. Drives the position back toward ``target_inventory``.
    """
    return config.k_inv * (net_yes - config.target_inventory)


def at_inventory_cap(net_yes: Decimal, config: MarketMakingConfig) -> bool:
    """True once ``|net_yes|`` reaches the hard cap (no more inventory-growing fills)."""
    return abs(net_yes) >= config.max_inventory


def quote_sizes(net_yes: Decimal, config: MarketMakingConfig) -> tuple[Decimal, Decimal]:
    """``(bid_size, ask_size)`` clamped to remaining headroom on each side.

    Buying YES at the bid grows ``net_yes``; selling at the ask shrinks it. Each
    side is capped by how much room is left before ``max_inventory``, so a fill can
    never push ``|net_yes|`` past the cap, and the inventory-growing side goes to 0
    at the cap (forcing single-sided quoting).
    """
    base = config.quote_size
    cap = config.max_inventory
    headroom_long = max(cap - net_yes, Decimal(0))  # room to buy more YES
    headroom_short = max(cap + net_yes, Decimal(0))  # room to sell more YES
    return min(base, headroom_long), min(base, headroom_short)


def resolution_stop(hours_to_resolution: float | None, config: MarketMakingConfig) -> bool:
    """True when too close to (or past) resolution to safely quote.

    Unknown resolution time (``None``) is treated as a stop — we don't quote a
    market whose settlement we can't time. Resolution is a jump to 0/1, so we
    flatten and stop rather than hold through it.
    """
    if hours_to_resolution is None:
        return True
    return Decimal(str(hours_to_resolution)) <= config.min_hours_to_resolution


def gamma_stop(gamma: float, config: MarketMakingConfig) -> bool:
    """True when pin risk exceeds the ceiling — pull quotes (don't just widen)."""
    return gamma >= float(config.gamma_ceiling)
