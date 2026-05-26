"""Volatility input for market making: the log-odds vol ``sigma``.

``sigma`` drives the quote half-spread (see ``strategy.py``). By default it is the
fixed value from config; when ``estimate_sigma`` is enabled it is estimated from a
rolling window of recent mids as the standard deviation of log-odds increments,
floored at ``sigma_floor`` and falling back to the configured value when history
is too thin to estimate.

The estimate is *per sample* (one increment per observation interval), so the
time unit of ``T`` passed to the pricing functions must match the mid sampling
interval for the spread to be dimensionally consistent. Returned as ``float`` —
it feeds the float pricing pipeline; money only becomes ``Decimal`` when a quote
price is quantized to the tick grid.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from polymarket_bot.common.config import MarketMakingConfig
from polymarket_bot.phase2_market_making.pricing import logit

# Need at least this many mids (two increments) for a meaningful sample stdev.
_MIN_SAMPLES = 3


def estimate_log_odds_vol(mids: Sequence[float]) -> float | None:
    """Sample stdev of log-odds increments over ``mids``; ``None`` if too short."""
    if len(mids) < _MIN_SAMPLES:
        return None
    xs = [logit(float(p)) for p in mids]
    increments = [b - a for a, b in zip(xs, xs[1:], strict=False)]
    return statistics.stdev(increments)


def sigma(config: MarketMakingConfig, history: Sequence[float] | None = None) -> float:
    """Resolve the log-odds volatility to use for quoting.

    Returns ``config.sigma`` unless estimation is enabled *and* ``history`` has
    enough samples, in which case the estimate is floored at ``config.sigma_floor``.
    """
    if not config.estimate_sigma or not history:
        return float(config.sigma)
    window = list(history)[-config.sigma_window :]
    estimate = estimate_log_odds_vol(window)
    if estimate is None:
        return float(config.sigma)
    return max(estimate, float(config.sigma_floor))
