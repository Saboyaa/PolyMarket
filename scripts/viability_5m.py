"""Is market making viable on a ~5-minute quote cadence? A reality check.

A maker posting a two-sided quote earns the spread but suffers *adverse
selection*: when the mid drifts, the favourable side fills and you're left
holding inventory that has already moved against you. Over a 5-minute quote
lifetime the question is simply:

    edge per fill  =  half_spread  −  fee  +  rebate
    risk per fill  =  typical 5-minute price move  (sigma_5m)
    VIABLE when     edge > risk

We measure ``sigma_5m`` directly from CLOB 5-minute price history, and compare
it against (a) the spread our model would *want* to quote and (b) the realistic
1-tick (1c) spread you can actually capture on a liquid market. Read-only.

    uv run python scripts/viability_5m.py [N]   # N = markets to sample (default 60)
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import UTC, datetime

import httpx

from polymarket_bot.common.fees import DEFAULT_REBATE, FeeSchedule, resolve_fee_rate
from polymarket_bot.phase2_market_making.pricing import pin_risk

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_FIDELITY_MIN = 5  # 5-minute bars
_INTERVAL = "1d"
_MID_LO, _MID_HI = 0.05, 0.95
_MIN_DAYS_TO_END = 14.0

# Calibrated model parameters (see scripts/calibrate_mm.py / config.example.toml).
_SIGMA = 6.7
_BASE_SPREAD = 0.005
_K_GAMMA = 0.0008
_TICK = 0.01  # realistic capture on a liquid 1c-spread market
_REBATE = float(DEFAULT_REBATE)


def _liquid_markets(n: int) -> list[dict]:
    r = httpx.get(
        f"{GAMMA}/markets",
        params={
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": min(n * 3, 300),
        },
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()


def _yes_token(m: dict) -> str | None:
    ids = m.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except json.JSONDecodeError:
            return None
    return ids[0] if isinstance(ids, (list, tuple)) and len(ids) == 2 else None


def _days_to_end(m: dict) -> float | None:
    raw = m.get("endDate") or m.get("end_date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - datetime.now(UTC)).total_seconds() / 86400.0


def _prices(token_id: str) -> list[float]:
    r = httpx.get(
        f"{CLOB}/prices-history",
        params={"market": token_id, "interval": _INTERVAL, "fidelity": str(_FIDELITY_MIN)},
        timeout=20.0,
    )
    if r.status_code != 200:
        return []
    return [float(pt["p"]) for pt in r.json().get("history", []) if "p" in pt]


def _sigma_5m(prices: list[float]) -> float | None:
    """Std of 5-minute price *changes* (price units) — the adverse-selection scale."""
    if len(prices) < 10:
        return None
    changes = [b - a for a, b in zip(prices, prices[1:], strict=False)]
    return statistics.pstdev(changes)


def _model_half_spread(p: float, days: float) -> float:
    return _BASE_SPREAD + _K_GAMMA * pin_risk(p, max(days, 0.01) / 365.25) * _SIGMA


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    fees = FeeSchedule.default()
    rate = float(resolve_fee_rate("Other", fees, datetime.now(UTC).date()))

    rows = []  # (q, p, sigma_5m, half, fee, rebate)
    for m in _liquid_markets(n):
        token = _yes_token(m)
        days = _days_to_end(m)
        if token is None or (days is not None and days < _MIN_DAYS_TO_END):
            continue
        prices = _prices(token)
        s5 = _sigma_5m(prices)
        if s5 is None:
            continue
        p = statistics.fmean(prices)
        if not (_MID_LO < p < _MID_HI):
            continue
        fee = rate * p * (1.0 - p)  # per-share USDC fee at this price
        rebate = _REBATE * fee
        half = _model_half_spread(p, days or 30)
        rows.append((m.get("question", "")[:42], p, s5, half, fee, rebate))
        if len(rows) >= n:
            break

    if not rows:
        print("No usable mid-range, longer-dated markets with 5-minute history found.")
        return 1

    print(f"Sampled {len(rows)} liquid mid-range markets (>{_MIN_DAYS_TO_END:.0f}d to resolution).")
    print("All figures in cents/share. edge = half_spread - fee + rebate; risk = 5-min move.\n")
    print(
        f"  {'p':>5} {'5m_move':>8} {'mdl_half':>9} {'fee':>5} "
        f"{'rbt':>5} {'net_mdl':>8} {'net_1c':>7}  market"
    )

    def c(x: float) -> float:
        return x * 100.0

    model_viable = tick_viable = 0
    edge_ratios = []
    for q, p, s5, half, fee, rebate in sorted(rows, key=lambda r: r[2]):
        net_model = half - fee + rebate
        net_tick = _TICK - fee + rebate  # realistic capture on a 1c market
        edge_ratios.append(net_model / s5 if s5 > 0 else float("inf"))
        if net_model > s5:
            model_viable += 1
        if net_tick > s5:
            tick_viable += 1
        flag = "OK" if net_model > s5 else "no"
        print(
            f"  {p:>5.2f} {c(s5):>8.3f} {c(half):>9.3f} {c(fee):>5.3f} {c(rebate):>5.3f} "
            f"{c(net_model):>8.3f} {c(net_tick):>7.3f}  {flag} {q}"
        )

    total = len(rows)
    med_ratio = statistics.median(edge_ratios)
    print("\n" + "=" * 60)
    print(" 5-MINUTE MARKET-MAKING VIABILITY")
    print("=" * 60)
    print(f"  markets where model edge  > 5-min move : {model_viable}/{total} "
          f"({100*model_viable/total:.0f}%)")
    print(f"  markets where 1c capture  > 5-min move : {tick_viable}/{total} "
          f"({100*tick_viable/total:.0f}%)")
    print(f"  median edge-to-noise ratio (model)     : {med_ratio:.2f}x")
    verdict = (
        "VIABLE — spread + rebate generally clears 5-minute drift."
        if med_ratio >= 1.0
        else "MARGINAL/NOT VIABLE — 5-minute drift tends to exceed the spread; "
        "adverse selection likely eats the edge. Quote wider or refresh slower."
    )
    print(f"\n  Verdict: {verdict}")
    print("=" * 60)
    print("Caveat: this assumes you capture your half-spread per fill and get")
    print("picked off by the full 5-min move — a rough envelope, not a backtest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
