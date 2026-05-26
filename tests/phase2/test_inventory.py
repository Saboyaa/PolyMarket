from decimal import Decimal

from polymarket_bot.common.config import MarketMakingConfig
from polymarket_bot.common.models import InventoryState, Side
from polymarket_bot.phase2_market_making.inventory import (
    apply_fill,
    at_inventory_cap,
    gamma_stop,
    position_delta,
    quote_sizes,
    resolution_stop,
    skew,
)


def _cfg(**kw):
    return MarketMakingConfig(**kw)


def test_position_delta_signs():
    s = Decimal("10")
    assert position_delta(Side.YES, buy=True, size=s) == Decimal("10")  # buy YES -> +
    assert position_delta(Side.YES, buy=False, size=s) == Decimal("-10")  # sell YES -> -
    assert position_delta(Side.NO, buy=True, size=s) == Decimal("-10")  # buy NO -> -
    assert position_delta(Side.NO, buy=False, size=s) == Decimal("10")  # sell NO -> +


def test_apply_fill_updates_position_and_fees():
    inv = InventoryState("c1")
    inv = apply_fill(inv, Side.YES, buy=True, size=Decimal("5"), fee=Decimal("0.02"))
    assert inv.net_yes == Decimal("5") and inv.fees_paid == Decimal("0.02")
    inv = apply_fill(inv, Side.YES, buy=False, size=Decimal("2"), fee=Decimal("0.01"))
    assert inv.net_yes == Decimal("3") and inv.fees_paid == Decimal("0.03")


def test_skew_pushes_toward_target():
    cfg = _cfg(k_inv="0.001", target_inventory="0")
    assert skew(Decimal("50"), cfg) == Decimal("0.050")  # long -> positive (quote lower)
    assert skew(Decimal("-50"), cfg) == Decimal("-0.050")  # short -> negative
    assert skew(Decimal("0"), cfg) == Decimal("0")


def test_skew_respects_nonzero_target():
    cfg = _cfg(k_inv="0.002", target_inventory="20")
    assert skew(Decimal("20"), cfg) == Decimal("0")  # at target -> no skew
    assert skew(Decimal("30"), cfg) == Decimal("0.020")


def test_inventory_cap():
    cfg = _cfg(max_inventory="100")
    assert at_inventory_cap(Decimal("100"), cfg) is True
    assert at_inventory_cap(Decimal("-100"), cfg) is True
    assert at_inventory_cap(Decimal("99"), cfg) is False


def test_quote_sizes_clamp_to_headroom_and_force_single_sided():
    cfg = _cfg(quote_size="10", max_inventory="100")
    # flat: both sides full
    assert quote_sizes(Decimal("0"), cfg) == (Decimal("10"), Decimal("10"))
    # at long cap: cannot buy more (bid 0), can still sell
    assert quote_sizes(Decimal("100"), cfg) == (Decimal("0"), Decimal("10"))
    # near long cap: bid shrinks to remaining headroom
    assert quote_sizes(Decimal("95"), cfg) == (Decimal("5"), Decimal("10"))
    # at short cap: cannot sell more (ask 0), can still buy
    assert quote_sizes(Decimal("-100"), cfg) == (Decimal("10"), Decimal("0"))


def test_quote_sizes_never_breach_cap():
    cfg = _cfg(quote_size="50", max_inventory="100")
    q = Decimal("80")
    bid_size, _ = quote_sizes(q, cfg)
    assert q + bid_size <= cfg.max_inventory  # a full bid fill can't exceed the cap


def test_resolution_stop():
    cfg = _cfg(min_hours_to_resolution="6")
    assert resolution_stop(6.0, cfg) is True  # at the boundary
    assert resolution_stop(3.0, cfg) is True
    assert resolution_stop(10.0, cfg) is False
    assert resolution_stop(None, cfg) is True  # unknown -> stop


def test_gamma_stop():
    cfg = _cfg(gamma_ceiling="5.0")
    assert gamma_stop(5.0, cfg) is True
    assert gamma_stop(7.3, cfg) is True
    assert gamma_stop(4.9, cfg) is False
