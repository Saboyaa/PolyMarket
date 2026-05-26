"""Effective-dated taker fee model and post-fee math.

Polymarket charges a **dynamic per-share taker fee**::

    fee_per_share = feeRate * p * (1 - p)
    leg_fee       = shares * fee_per_share

charged in USDC with 5-decimal precision, minimum 0.00001 when any fee is
owed. The fee peaks at ``p = 0.5`` and is symmetric (a 30c trade pays the same
as a 70c trade). Both legs of an arb buy at the ask, so both pay a taker fee.

The per-category ``feeRate`` table is kept as *data* and is both
**effective-dated** (Polymarket has changed rates over time) and
**config-overridable** (operator escape hatch). All money is :class:`Decimal`;
never float.

Sources: see ``docs/spec.md`` (Fee Model section). Rates seeded from the
March 2026 fee change; re-verify before any live trading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from polymarket_bot.common.config import FeesConfig

# Category used when a market's category is unknown / unmapped.
DEFAULT_CATEGORY = "Other"

# 5-decimal money quantum and minimum charged fee.
FEE_QUANTUM = Decimal("0.00001")
MIN_FEE = Decimal("0.00001")

# --- Seed data: the March 2026 fee change --------------------------------

MARCH_2026_EFFECTIVE = date(2026, 3, 1)

#: Per-category taker ``feeRate`` in effect from the March 2026 change.
MARCH_2026_RATES: dict[str, Decimal] = {
    "Crypto": Decimal("0.07"),
    "Sports": Decimal("0.03"),
    "Finance": Decimal("0.04"),
    "Politics": Decimal("0.04"),
    "Tech": Decimal("0.04"),
    "Mentions": Decimal("0.04"),
    "Economics": Decimal("0.05"),
    "Culture": Decimal("0.05"),
    "Weather": Decimal("0.05"),
    "Other": Decimal("0.05"),
    "Geopolitics": Decimal("0"),
}

#: Maker rebate fraction per category (data only; used by Phase 2 market making).
MARCH_2026_REBATES: dict[str, Decimal] = {
    "Crypto": Decimal("0.20"),
    "Sports": Decimal("0.25"),
    "Finance": Decimal("0.25"),
    "Politics": Decimal("0.25"),
    "Tech": Decimal("0.25"),
    "Mentions": Decimal("0.25"),
    "Economics": Decimal("0.25"),
    "Culture": Decimal("0.25"),
    "Weather": Decimal("0.25"),
    "Other": Decimal("0.25"),
    "Geopolitics": Decimal("0"),
}

# Default maker rebate for an unmapped fee-bearing category.
DEFAULT_REBATE = Decimal("0.25")


@dataclass(frozen=True)
class FeeSchedule:
    """An effective-dated sequence of per-category ``feeRate`` tables.

    ``entries`` is a list of ``(effective_from, {category: rate})``. The active
    table for a given date is the entry with the latest ``effective_from`` that
    is on or before that date (effective dates are inclusive).
    """

    entries: list[tuple[date, dict[str, Decimal]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.entries:
            raise ValueError("FeeSchedule requires at least one entry")
        # Sort by effective date so selection is a simple scan; entries is a
        # plain list so we mutate via object.__setattr__ to respect frozen.
        ordered = sorted(self.entries, key=lambda e: e[0])
        object.__setattr__(self, "entries", ordered)

    @classmethod
    def default(cls) -> FeeSchedule:
        """The seeded schedule (March 2026 rates effective from the change)."""
        return cls(entries=[(MARCH_2026_EFFECTIVE, dict(MARCH_2026_RATES))])

    def rates_on(self, as_of: date) -> dict[str, Decimal]:
        """Return the active per-category rate table for ``as_of``."""
        active: dict[str, Decimal] | None = None
        for effective_from, table in self.entries:
            if effective_from <= as_of:
                active = table
            else:
                break
        if active is None:
            raise ValueError(f"no fee schedule entry effective on or before {as_of.isoformat()}")
        return active


def resolve_fee_rate(
    category: str,
    schedule: FeeSchedule,
    as_of: date,
    fees_config: FeesConfig | None = None,
) -> Decimal:
    """Resolve the taker ``feeRate`` for ``category`` on ``as_of``.

    A config ``rate_override`` (if it names the category, case-insensitively)
    takes precedence over the schedule. Unknown categories fall back to the
    ``Other`` rate. Lookups are case-insensitive.
    """
    if fees_config is not None and fees_config.rate_override:
        override = {k.casefold(): v for k, v in fees_config.rate_override.items()}
        hit = override.get(category.casefold())
        if hit is not None:
            return hit

    table = schedule.rates_on(as_of)
    lookup = {k.casefold(): v for k, v in table.items()}
    hit = lookup.get(category.casefold())
    if hit is not None:
        return hit
    # Fall back to the default category rate.
    return lookup[DEFAULT_CATEGORY.casefold()]


def per_share_fee(rate: Decimal, price: Decimal) -> Decimal:
    """Unrounded per-share fee: ``rate * p * (1 - p)``."""
    return rate * price * (Decimal(1) - price)


def leg_fee(shares: Decimal, rate: Decimal, price: Decimal) -> Decimal:
    """Total USDC fee for one leg, rounded to 5 dp with a min-fee floor.

    Returns exactly ``Decimal("0")`` when no fee is owed (zero rate or zero
    size); the min-fee floor only applies to a genuinely positive fee that
    would otherwise round below ``MIN_FEE``.
    """
    raw = shares * per_share_fee(rate, price)
    if raw <= 0:
        return Decimal(0)
    rounded = raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_EVEN)
    if rounded < MIN_FEE:
        return MIN_FEE
    return rounded


def rebate_rate(category: str) -> Decimal:
    """Maker rebate fraction for ``category`` (data only; Phase 2)."""
    lookup = {k.casefold(): v for k, v in MARCH_2026_REBATES.items()}
    hit = lookup.get(category.casefold())
    return hit if hit is not None else DEFAULT_REBATE
