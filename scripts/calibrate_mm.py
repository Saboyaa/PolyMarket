"""Calibrate Phase 2 market-making parameters from real Polymarket data.

"Training" for this bot means estimating the model's one genuinely empirical
input — ``sigma``, the volatility of the log-odds — from historical prices, then
deriving sensible spread/risk constants from it. The rest (``k_gamma``,
``gamma_ceiling``) are risk-appetite knobs we set so the resulting spread and
stops are reasonable given the measured ``sigma``.

Method:
  1. pull the most-liquid active markets (Gamma, by 24h volume);
  2. fetch each YES token's price history (CLOB ``prices-history``);
  3. per market, estimate per-bar log-odds vol (reusing
     ``volatility.estimate_log_odds_vol``) and annualize it by the bar interval;
  4. report the distribution and print a recommended ``[mm]`` config block.

Read-only; places no orders.

    uv run python scripts/calibrate_mm.py [N]   # N = markets to sample (default 60)
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from polymarket_bot.phase2_market_making.pricing import pin_risk
from polymarket_bot.phase2_market_making.volatility import estimate_log_odds_vol

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_FIDELITY_MIN = 60  # minutes per history bar
_INTERVAL = "1w"  # history window
_BARS_PER_YEAR = 365.25 * 24 * 60 / _FIDELITY_MIN
_MID_LO, _MID_HI = 0.05, 0.95  # "mid-range" markets MM actually quotes
_BASE_SPREAD = 0.005  # assumed floor half-spread for the k_gamma derivation
_TARGET_HALF = 0.02  # desired half-spread at p=0.5, 7 days out (2 cents)
_MIN_DAYS_TO_END = 14.0  # skip short-dated markets (sports/esports): bad MM targets


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


def _liquid_markets(n: int) -> list[dict]:
    r = httpx.get(
        f"{GAMMA}/markets",
        params={
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": min(n * 2, 200),
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


def _price_history(token_id: str) -> list[float]:
    r = httpx.get(
        f"{CLOB}/prices-history",
        params={"market": token_id, "interval": _INTERVAL, "fidelity": str(_FIDELITY_MIN)},
        timeout=20.0,
    )
    if r.status_code != 200:
        return []
    return [float(pt["p"]) for pt in r.json().get("history", []) if "p" in pt]


def _annualized_sigma(prices: list[float]) -> float | None:
    per_bar = estimate_log_odds_vol(prices)
    if per_bar is None:
        return None
    return per_bar * (_BARS_PER_YEAR**0.5)


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    markets = _liquid_markets(n)

    rows: list[tuple[str, float, float, int]] = []  # (question, mean_p, sigma_ann, points)
    skipped_short = 0
    for m in markets:
        token = _yes_token(m)
        if token is None:
            continue
        days = _days_to_end(m)
        if days is not None and days < _MIN_DAYS_TO_END:
            skipped_short += 1
            continue  # short-dated: huge jump risk, not an MM target
        prices = _price_history(token)
        if len(prices) < 10:
            continue
        sigma = _annualized_sigma(prices)
        if sigma is None:
            continue
        rows.append((m.get("question", "")[:48], statistics.fmean(prices), sigma, len(prices)))
        if len(rows) >= n:
            break

    if not rows:
        print("No markets with usable price history found.")
        return 1
    print(f"(skipped {skipped_short} markets resolving in < {_MIN_DAYS_TO_END:.0f} days)")

    all_sigmas = sorted(s for _, _, s, _ in rows)
    mid_sigmas = sorted(s for _, mp, s, _ in rows if _MID_LO < mp < _MID_HI)

    print(f"Sampled {len(rows)} liquid markets ({_INTERVAL} history, {_FIDELITY_MIN}m bars).\n")
    print("Widest movers (highest annualized log-odds vol):")
    for q, mp, s, npts in sorted(rows, key=lambda r: -r[2])[:10]:
        print(f"  sigma={s:7.2f}  mean_p={mp:5.3f}  n={npts:3d}  {q}")

    def _stats(label: str, xs: list[float]) -> float | None:
        if not xs:
            print(f"\n{label}: none")
            return None
        med = statistics.median(xs)
        print(
            f"\n{label} (n={len(xs)}): "
            f"median={med:.2f}  mean={statistics.fmean(xs):.2f}  "
            f"min={xs[0]:.2f}  max={xs[-1]:.2f}"
        )
        return med

    _stats("All markets, annualized sigma", all_sigmas)
    sigma_reco = _stats("Mid-range markets (0.05<p<0.95)", mid_sigmas) or statistics.median(
        all_sigmas
    )

    # --- Derive spread/risk constants from the measured sigma --------------
    pin_7d = pin_risk(0.5, 7.0 / 365.25)  # reference pin risk at the money, 7 days out
    k_gamma = max(0.0, (_TARGET_HALF - _BASE_SPREAD) / (pin_7d * sigma_reco))
    # Pull quotes when at-the-money risk exceeds the ~24h-out level.
    gamma_ceiling = pin_risk(0.5, 1.0 / 365.25) * sigma_reco

    def q(x: float) -> Decimal:
        return Decimal(str(round(x, 4)))

    print("\n" + "=" * 60)
    print(" Recommended [mm] config (data-driven sigma; tune to taste):")
    print("=" * 60)
    print("[mm]")
    print(f"sigma = {q(sigma_reco)}          # median annualized log-odds vol")
    print(f"base_spread = {q(_BASE_SPREAD)}")
    print(f"k_gamma = {q(k_gamma)}        # ~{_TARGET_HALF:.0%}/2 half-spread at p=0.5, 7d out")
    print(f"gamma_ceiling = {q(gamma_ceiling)}   # pulls quotes ~24h-equivalent at-money risk")
    print("estimate_sigma = true     # let the runner refine sigma per market online")
    print("=" * 60)
    print("Note: constants are placeholders to start from, not gospel. Backtest /")
    print("paper-run before trusting them, and re-run after market regimes shift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
