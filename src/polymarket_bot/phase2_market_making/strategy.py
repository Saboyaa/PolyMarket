"""Quote construction — the heart of Phase 2 (pure, no I/O).

``build_quotes`` turns a book snapshot plus market state into a two-sided
:class:`Quote`, or ``None`` when a stop says pull quotes:

    reservation price  r_p = mid − skew(inventory)
    half-spread        δ   = base_spread + k_gamma · (pin_risk · sigma)
    YES bid / ask          = r_p ∓ δ, quantized to the tick grid, clamped to (0,1)

Sizes come from :func:`inventory.quote_sizes`, so the inventory-growing side turns
off at the cap (single-sided quoting). The Black-Scholes math runs in ``float``;
every price crossing into a :class:`Quote` is quantized back to ``Decimal``.

``hours_to_resolution`` is the single time input; ``T`` (years) is derived from it
so the resolution stop (config hours) and the pin-risk term share one source.
``sigma`` is the per-year log-odds volatility from :mod:`phase2.volatility`.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from polymarket_bot.common.config import MarketMakingConfig
from polymarket_bot.common.models import InventoryState, OrderBook, Quote, Side
from polymarket_bot.phase2_market_making.inventory import (
    gamma_stop,
    quote_sizes,
    resolution_stop,
    skew,
)
from polymarket_bot.phase2_market_making.pricing import pin_risk

_HOURS_PER_YEAR = 24.0 * 365.25  # 8766
_SECONDS_PER_YEAR = _HOURS_PER_YEAR * 3600.0


def quote_cadence_seconds(
    mid: Decimal,
    sigma: float,
    half_spread: Decimal,
    config: MarketMakingConfig,
) -> float:
    """Requote interval where expected drift ≈ the quote half-spread.

    A maker quote rests ``half_spread`` from the mid, so it only fills once the
    mid drifts that far. Price drift over horizon ``h`` (years) is
    ``≈ p(1−p)·sigma·√h`` (log-odds vol mapped to price by the link slope), so the
    cadence at which drift equals the spread is ``h = (δ / (p(1−p)·sigma))²``.
    Calm markets get long cadences, fast ones short; clamped to the config bounds.
    """
    lo, hi = float(config.min_cadence_seconds), float(config.max_cadence_seconds)
    p = float(mid)
    slope = p * (1.0 - p)
    if slope <= 0 or sigma <= 0 or half_spread <= 0:
        return hi
    sqrt_h_years = float(half_spread) / (slope * sigma)
    seconds = (sqrt_h_years**2) * _SECONDS_PER_YEAR
    return min(max(seconds, lo), hi)


def _on_grid(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    """Snap ``price`` to the tick grid using the given rounding mode."""
    return (price / tick).to_integral_value(rounding=rounding) * tick


def build_quotes(
    book: OrderBook,
    hours_to_resolution: float | None,
    sigma: float,
    inventory: InventoryState,
    config: MarketMakingConfig,
) -> Quote | None:
    """Build a YES quote for ``book``, or ``None`` if a stop fires.

    Stops: missing mid (no two-sided book), resolution stop (too close to/past
    settlement), gamma stop (pin risk over ceiling), and inventory exhaustion
    (both sides clamped to zero size).
    """
    mid = book.mid_price(Side.YES)
    if mid is None:
        return None
    if resolution_stop(hours_to_resolution, config):
        return None

    # hours_to_resolution is not None here (resolution_stop returns True for None).
    assert hours_to_resolution is not None
    p = float(mid)
    t_years = hours_to_resolution / _HOURS_PER_YEAR
    effective_gamma = pin_risk(p, t_years) * sigma  # the risk dial used for spread + stop
    if gamma_stop(effective_gamma, config):
        return None

    half = float(config.base_spread) + float(config.k_gamma) * effective_gamma
    reservation = mid - skew(inventory.net_yes, config)

    half_dec = Decimal(str(half))
    tick = config.tick_size
    # Quote no tighter than computed: floor the bid, ceil the ask onto the grid.
    bid = _on_grid(reservation - half_dec, tick, ROUND_FLOOR)
    ask = _on_grid(reservation + half_dec, tick, ROUND_CEILING)

    # Clamp into (0, 1) on the grid and guarantee bid < ask by at least one tick.
    lo, hi = tick, Decimal(1) - tick
    bid = min(max(bid, lo), hi - tick)
    ask = min(max(ask, bid + tick), hi)

    bid_size, ask_size = quote_sizes(inventory.net_yes, config)
    if bid_size <= 0 and ask_size <= 0:
        return None

    return Quote(
        condition_id=book.condition_id,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
    )
