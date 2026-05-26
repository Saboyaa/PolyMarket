"""Intra-market arbitrage detection.

If we can buy one YES share and one NO share for less than the guaranteed
$1.00 payout — *after* taker fees on both legs — we lock in risk-free profit.

``find_intramarket_arb`` prices both legs at the best ask and sizes against the
consumable depth available at those best levels on *both* sides (we can only
hedge as many shares as the thinner side supplies at the quoted price). It
returns a sized :class:`Opportunity` only when the post-fee net edge per share
meets ``min_net_edge_per_share``; otherwise ``None``.

All money is :class:`Decimal`. The fee math is delegated to ``common.fees`` so
net-edge is computed identically everywhere.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from polymarket_bot.common.config import FeesConfig
from polymarket_bot.common.fees import FeeSchedule, per_share_fee, resolve_fee_rate
from polymarket_bot.common.models import Opportunity, OrderBook, Side


def find_intramarket_arb(
    book: OrderBook,
    fees: FeeSchedule,
    min_net_edge_per_share: Decimal,
    category: str,
    as_of: date,
    fees_config: FeesConfig | None = None,
) -> Opportunity | None:
    """Detect a YES+NO arbitrage in ``book``.

    Returns a sized :class:`Opportunity` when buying both sides at the best ask
    nets at least ``min_net_edge_per_share`` after fees, else ``None``.
    """
    yes = book.best_ask(Side.YES)
    no = book.best_ask(Side.NO)
    if yes is None or no is None:
        return None

    # Consumable depth at the best level on each side; we can only pair the
    # smaller of the two.
    size = min(yes.size, no.size)
    if size <= 0:
        return None

    yes_ask = yes.price
    no_ask = no.price
    gross_edge = Decimal(1) - (yes_ask + no_ask)
    if gross_edge <= 0:
        return None

    rate = resolve_fee_rate(category, fees, as_of, fees_config=fees_config)
    fee_per_share = per_share_fee(rate, yes_ask) + per_share_fee(rate, no_ask)
    net_edge = gross_edge - fee_per_share

    if net_edge < min_net_edge_per_share:
        return None

    return Opportunity(
        condition_id=book.condition_id,
        category=category,
        yes_ask=yes_ask,
        no_ask=no_ask,
        size=size,
        gross_edge_per_share=gross_edge,
        net_edge_per_share=net_edge,
    )
