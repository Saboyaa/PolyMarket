import math
import statistics

from polymarket_bot.common.config import MarketMakingConfig
from polymarket_bot.phase2_market_making.pricing import logistic
from polymarket_bot.phase2_market_making.volatility import (
    estimate_log_odds_vol,
    sigma,
)


def test_config_passthrough_when_estimation_off():
    cfg = MarketMakingConfig(sigma="0.7", estimate_sigma=False)
    # even with ample history, the fixed value is used
    assert sigma(cfg, history=[0.4, 0.5, 0.6, 0.55]) == 0.7


def test_passthrough_when_no_history():
    cfg = MarketMakingConfig(sigma="0.9", estimate_sigma=True)
    assert sigma(cfg, history=None) == 0.9
    assert sigma(cfg, history=[]) == 0.9


def test_fallback_on_thin_history():
    cfg = MarketMakingConfig(sigma="0.6", estimate_sigma=True)
    assert sigma(cfg, history=[0.5, 0.5]) == 0.6  # < 3 samples -> config


def test_estimate_matches_hand_computation():
    # build mids whose log-odds are 0, 0.1, 0.4 => increments [0.1, 0.3]
    mids = [logistic(0.0), logistic(0.1), logistic(0.4)]
    expected = statistics.stdev([0.1, 0.3])
    cfg = MarketMakingConfig(sigma="99", estimate_sigma=True, sigma_floor="0.01")
    got = sigma(cfg, history=mids)
    assert math.isclose(got, expected, rel_tol=1e-9)


def test_floor_applied_to_low_estimate():
    mids = [logistic(0.0), logistic(0.0001), logistic(0.0002)]  # near-flat
    cfg = MarketMakingConfig(sigma="99", estimate_sigma=True, sigma_floor="0.2")
    assert sigma(cfg, history=mids) == 0.2


def test_window_limits_samples_used():
    # a long stale flat prefix is dropped; only the last `window` mids count
    cfg = MarketMakingConfig(sigma="99", estimate_sigma=True, sigma_window=3, sigma_floor="0.01")
    mids = [0.5] * 10 + [logistic(0.0), logistic(0.1), logistic(0.4)]
    expected = statistics.stdev([0.1, 0.3])
    assert math.isclose(sigma(cfg, history=mids), expected, rel_tol=1e-9)


def test_estimate_log_odds_vol_none_when_short():
    assert estimate_log_odds_vol([0.5, 0.5]) is None
