"""Pick the best quoting cadence per market (5m / 1h / 6h / 1d / 2d).

A maker quote rests a half-spread ``delta`` from the mid, so it only fills once
the mid drifts ~``delta``. Over a horizon ``h`` the typical drift is ``sigma_h``
(measured from CLOB price history). The best requote cadence is the horizon where
``sigma_h`` is closest to ``delta``:

  * refresh much faster  (sigma_h << delta): the price never reaches your quote
    within the horizon -> you almost never fill;
  * refresh much slower  (sigma_h >> delta): the market blows through your spread
    between requotes -> you're picked off (adverse selection).

So per market we sweep the horizons, compute ``sigma_h`` at each, and choose the
one minimizing ``|sigma_h - delta|``. Read-only; places no orders.

    uv run python scripts/best_horizon_mm.py [N]   # N = markets (default 40)
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime

import httpx

from polymarket_bot.phase2_market_making.pricing import pin_risk

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
_MID_LO, _MID_HI, _MIN_DAYS = 0.05, 0.95, 14.0

# Calibrated model (scripts/calibrate_mm.py).
_SIGMA, _BASE_SPREAD, _K_GAMMA = 6.7, 0.005, 0.0008

# Candidate cadences, in minutes.
_HORIZONS = [("5m", 5), ("1h", 60), ("6h", 360), ("1d", 1440), ("2d", 2880)]


def _markets(n: int) -> list[dict]:
    r = httpx.get(
        f"{GAMMA}/markets",
        params={"active": "true", "closed": "false", "order": "volume24hr",
                "ascending": "false", "limit": 300},
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()


def _token(m: dict) -> str | None:
    ids = m.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except json.JSONDecodeError:
            return None
    return ids[0] if isinstance(ids, (list, tuple)) and len(ids) == 2 else None


def _days(m: dict) -> float | None:
    raw = m.get("endDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - datetime.now(UTC)).total_seconds() / 86400.0


def _last_price(m: dict) -> float | None:
    if m.get("lastTradePrice") is not None:
        try:
            return float(m["lastTradePrice"])
        except (TypeError, ValueError):
            pass
    raw = m.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, (list, tuple)) and raw:
        try:
            return float(raw[0])
        except (TypeError, ValueError):
            return None
    return None


def _series(token: str, interval: str, fidelity: int) -> list[float]:
    r = httpx.get(
        f"{CLOB}/prices-history",
        params={"market": token, "interval": interval, "fidelity": str(fidelity)},
        timeout=20.0,
    )
    if r.status_code != 200:
        return []
    return [float(pt["p"]) for pt in r.json().get("history", []) if "p" in pt]


def _sigma_over(prices: list[float], step: int) -> float | None:
    """Std of price changes ``step`` bars apart."""
    if len(prices) <= step:
        return None
    changes = [prices[i + step] - prices[i] for i in range(len(prices) - step)]
    return statistics.pstdev(changes) if len(changes) > 1 else None


def _sigma_by_horizon(token: str) -> dict[str, float]:
    fine = _series(token, "1d", 5)  # 5-minute bars over a day
    hourly = _series(token, "1w", 60)  # hourly bars over a week
    out: dict[str, float] = {}
    for label, minutes in _HORIZONS:
        if minutes <= 60 and fine:
            sig = _sigma_over(fine, max(minutes // 5, 1))
        elif hourly:
            sig = _sigma_over(hourly, max(minutes // 60, 1))
        else:
            sig = None
        if sig is not None:
            out[label] = sig
    return out


def _half_spread(p: float, days: float) -> float:
    return _BASE_SPREAD + _K_GAMMA * pin_risk(p, max(days, 0.01) / 365.25) * _SIGMA


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40

    print("Per-market best quoting cadence (delta = model half-spread; sigma_h = drift).")
    print("All figures in cents/share.\n")
    labels = [lab for lab, _ in _HORIZONS]
    cols = " ".join(f"{lab:>7}" for lab in labels)
    print(f"  {'p':>5} {'delta':>6} {cols}   best  market")

    best_counter: Counter[str] = Counter()
    rows = 0
    for m in _markets(n):
        token = _token(m)
        d = _days(m)
        p = _last_price(m)
        if token is None or d is None or d < _MIN_DAYS or p is None or not (_MID_LO < p < _MID_HI):
            continue
        sig = _sigma_by_horizon(token)
        if len(sig) < 2:
            continue
        delta = _half_spread(p, d)
        # best cadence: horizon whose drift is closest to our quote distance.
        best = min(sig, key=lambda h: abs(sig[h] - delta))
        best_counter[best] += 1
        cells = " ".join(f"{(sig.get(lab, float('nan')) * 100):>7.3f}" for lab in labels)
        print(f"  {p:>5.2f} {delta * 100:>6.3f} {cells}   {best:>4}  {m.get('question', '')[:34]}")
        rows += 1
        if rows >= n:
            break

    if not rows:
        print("No usable markets found.")
        return 1

    print("\n" + "=" * 56)
    print(" Best-cadence distribution across sampled markets:")
    for lab, _ in _HORIZONS:
        c = best_counter.get(lab, 0)
        bar = "#" * c
        print(f"   {lab:>3}: {c:>3}  {bar}")
    print("=" * 56)
    top = best_counter.most_common(1)[0][0] if best_counter else "n/a"
    print(f"Most common best cadence: {top}. Match your quote-refresh interval to")
    print("each market's drift: fast markets want longer cadences, calm ones shorter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
