"""Black-Scholes binary-option math for market making (pure, stdlib only).

A Polymarket YES share is a cash-or-nothing binary that pays $1 if the event
resolves true. Its price *is* the risk-neutral probability ``p``. We model a
latent Gaussian state ``x`` (the Black-Scholes "underlying") with ``p = Phi(x)``
— i.e. ``p = N(d2)`` — so the observed mid gives ``x = Phi^{-1}(p)``. Time to
resolution is ``T`` (same time unit as the volatility ``sigma``); this module is
unit-agnostic and takes ``T`` as a float.

Everything here is ``float`` and inherently approximate; callers quantize prices
to the tick grid as ``Decimal`` before quoting (money never lives as float).

A note on the "gamma" name. For market making the dangerous quantity near
resolution is how violently fair value moves for a small move in the latent
state — :func:`pin_risk` below. For a *binary* that sensitivity is the option's
delta-to-state (peaks at ``p = 0.5``, blows up as ``T -> 0``), not its textbook
gamma (which is actually zero at the money). We keep the config names
``k_gamma`` / ``gamma_ceiling`` for continuity but :func:`gamma_proxy` is an
alias for this pin-risk measure, documented so the math stays honest.
"""

from __future__ import annotations

import math
from statistics import NormalDist

_N = NormalDist()  # standard normal; .pdf, .cdf, .inv_cdf are stdlib (3.8+)

# Clamp p away from {0, 1} so Phi^{-1}(p) stays finite.
_EPS = 1e-12


def _clamp_p(p: float) -> float:
    return min(max(p, _EPS), 1.0 - _EPS)


def logit(p: float) -> float:
    """Log-odds ``ln(p / (1 - p))``. Inverse of :func:`logistic`."""
    p = _clamp_p(p)
    return math.log(p / (1.0 - p))


def logistic(x: float) -> float:
    """Logistic ``1 / (1 + e^-x)``, mapping the real line into (0, 1)."""
    return 1.0 / (1.0 + math.exp(-x))


def pin_risk(p: float, T: float) -> float:
    """Sensitivity of the binary's fair value to a move in the latent state.

    ``= phi(Phi^{-1}(p)) / sqrt(T)`` (at unit volatility; ``sigma`` is applied by
    the caller). This is the market-making risk dial:

    * ``-> 0`` as ``T -> inf`` (far from resolution, fair value barely moves),
    * ``-> +inf`` as ``T -> 0`` with ``p`` mid-range (the value curve collapses to
      a step at the strike — resting quotes get picked off),
    * symmetric about ``p = 0.5`` and maximal there,
    * monotonically increasing as ``T`` shrinks (at fixed mid-range ``p``).

    ``T <= 0`` returns ``inf`` (at/after resolution: pull quotes).
    """
    if T <= 0.0:
        return math.inf
    d = _N.inv_cdf(_clamp_p(p))
    return _N.pdf(d) / math.sqrt(T)


# Config and strategy speak of "gamma"; expose pin_risk under that name too.
gamma_proxy = pin_risk


def link_slope(p: float) -> float:
    """Local slope ``dp/dx = p(1 - p)`` of the log-odds link (a true binary delta
    proxy, time-independent). Maximal at ``p = 0.5``, ``-> 0`` at the extremes."""
    p = _clamp_p(p)
    return p * (1.0 - p)
