"""Pre-trade risk caps.

``apply_caps`` enforces **both** the per-trade notional cap and the remaining
total-exposure headroom before any order (paper or live). An opportunity that
exceeds either limit is sized down to the largest viable size; if no positive
size fits, it returns ``None``.

Notional here is the USDC outlay to open *both* legs:
``size * (yes_ask + no_ask)``. All money is :class:`Decimal`.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from polymarket_bot.common.config import RiskConfig
from polymarket_bot.common.models import Opportunity


def apply_caps(
    opportunity: Opportunity,
    open_exposure: Decimal,
    risk_config: RiskConfig,
) -> Opportunity | None:
    """Size ``opportunity`` down to fit per-trade and total-exposure caps.

    Returns the (possibly resized) opportunity, or ``None`` if no positive
    size is viable.
    """
    headroom = risk_config.max_total_exposure - open_exposure
    notional_cap = min(risk_config.max_trade_notional, headroom)
    if notional_cap <= 0:
        return None

    price_per_pair = opportunity.yes_ask + opportunity.no_ask

    # Zero-cost pair: any size fits under a positive cap; keep original size.
    if price_per_pair <= 0:
        return opportunity

    max_size = notional_cap / price_per_pair
    if max_size <= 0:
        return None

    if opportunity.size <= max_size:
        return opportunity

    return replace(opportunity, size=max_size)
