import math

import pytest

from polymarket_bot.phase2_market_making.pricing import (
    gamma_proxy,
    link_slope,
    logistic,
    logit,
    pin_risk,
)


def test_logit_logistic_round_trip():
    for p in (0.01, 0.1, 0.37, 0.5, 0.63, 0.9, 0.99):
        assert math.isclose(logistic(logit(p)), p, rel_tol=1e-9, abs_tol=1e-12)


def test_logit_symmetric_about_half():
    assert math.isclose(logit(0.7), -logit(0.3), abs_tol=1e-12)
    assert math.isclose(logit(0.5), 0.0, abs_tol=1e-12)


def test_pin_risk_symmetric_about_half():
    T = 0.5
    assert math.isclose(pin_risk(0.3, T), pin_risk(0.7, T), rel_tol=1e-12)
    assert math.isclose(pin_risk(0.1, T), pin_risk(0.9, T), rel_tol=1e-12)


def test_pin_risk_maximal_at_half():
    T = 0.5
    assert pin_risk(0.5, T) > pin_risk(0.4, T) > pin_risk(0.2, T) > pin_risk(0.05, T)


def test_pin_risk_vanishes_as_T_grows():
    assert pin_risk(0.5, 1e6) < 1e-2
    # monotonic: smaller T -> larger pin risk at the money
    assert pin_risk(0.5, 0.01) > pin_risk(0.5, 0.1) > pin_risk(0.5, 1.0) > pin_risk(0.5, 100.0)


def test_pin_risk_explodes_near_resolution():
    assert pin_risk(0.5, 1e-6) > 100.0
    assert pin_risk(0.5, 0.0) == math.inf
    assert pin_risk(0.5, -1.0) == math.inf


def test_pin_risk_extremes_are_small():
    # far from the pin, fair value is robust even close to expiry
    assert pin_risk(0.001, 0.01) < pin_risk(0.5, 0.01)


def test_gamma_proxy_is_pin_risk_alias():
    assert gamma_proxy(0.5, 0.5) == pin_risk(0.5, 0.5)


def test_link_slope():
    assert math.isclose(link_slope(0.5), 0.25)
    assert link_slope(0.5) > link_slope(0.2) > link_slope(0.02)
    assert math.isclose(link_slope(0.3), link_slope(0.7))


def test_clamp_keeps_extremes_finite():
    # p at the boundary must not raise
    assert math.isfinite(pin_risk(0.0, 0.5))
    assert math.isfinite(pin_risk(1.0, 0.5))
    assert math.isfinite(logit(0.0)) and math.isfinite(logit(1.0))


@pytest.mark.parametrize("p", [0.5, 0.25, 0.75])
def test_pin_risk_positive(p):
    assert pin_risk(p, 0.5) > 0.0
