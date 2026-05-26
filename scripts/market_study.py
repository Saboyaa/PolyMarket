"""Where does the Black-Scholes MM strategy work best? A cross-market study.

Samples active markets across categories (Gamma events, by volume), and for each
measures the two things that decide market-making profitability:

  * net edge per fill  = half_spread - fee + rebate   (what you earn, calibrated)
  * drift (sigma_1h)   = std of hourly price moves      (adverse-selection risk)

Their ratio (edge-to-noise) is the headline: >1 means the spread you'd quote
clears the typical drift. We then aggregate by category, time-to-resolution, and
price level to show which *segments* the strategy suits. Read-only.

    uv run python scripts/market_study.py [N]   # N = markets to analyze (default 100)
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime

import httpx

from polymarket_bot.common.clients.gamma import _event_category
from polymarket_bot.common.fees import FeeSchedule, rebate_rate, resolve_fee_rate
from polymarket_bot.phase2_market_making.pricing import pin_risk

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
_MID_LO, _MID_HI = 0.05, 0.95
_SIGMA, _BASE_SPREAD, _K_GAMMA = 6.7, 0.005, 0.0008  # calibrated model
_TODAY = datetime.now(UTC).date()
_FEES = FeeSchedule.default()


class Row:
    __slots__ = ("cat", "p", "days", "vol", "edge", "drift", "ratio")

    def __init__(self, cat, p, days, vol, edge, drift):
        self.cat, self.p, self.days, self.vol = cat, p, days, vol
        self.edge, self.drift = edge, drift
        self.ratio = edge / drift if drift > 0 else float("inf")


def _events(pages: int) -> list[dict]:
    out: list[dict] = []
    for page in range(pages):
        r = httpx.get(
            f"{GAMMA}/events",
            params={"active": "true", "closed": "false", "order": "volume24hr",
                    "ascending": "false", "limit": 100, "offset": page * 100},
            timeout=20.0,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
    return out


def _token_and_price(m: dict) -> tuple[str, float] | None:
    ids = m.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except json.JSONDecodeError:
            return None
    if not (isinstance(ids, (list, tuple)) and len(ids) == 2):
        return None
    p = m.get("lastTradePrice")
    try:
        return ids[0], float(p)
    except (TypeError, ValueError):
        return None


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


def _drift_1h(token: str) -> float | None:
    for attempt in range(2):  # one retry; a single slow call must not abort the study
        try:
            r = httpx.get(f"{CLOB}/prices-history",
                          params={"market": token, "interval": "1w", "fidelity": "60"},
                          timeout=15.0)
        except httpx.HTTPError:
            if attempt == 0:
                continue
            return None
        break
    if r.status_code != 200:
        return None
    prices = [float(pt["p"]) for pt in r.json().get("history", []) if "p" in pt]
    if len(prices) < 12:
        return None
    changes = [b - a for a, b in zip(prices, prices[1:], strict=False)]
    return statistics.pstdev(changes)


def _net_edge(cat: str, p: float, days: float) -> float:
    half = _BASE_SPREAD + _K_GAMMA * pin_risk(p, max(days, 0.01) / 365.25) * _SIGMA
    rate = float(resolve_fee_rate(cat, _FEES, _TODAY))
    fee = rate * p * (1.0 - p)
    rebate = float(rebate_rate(cat)) * fee
    return half - fee + rebate


def _days_bucket(d: float) -> str:
    if d < 7:
        return "<7d"
    if d < 30:
        return "7-30d"
    if d < 90:
        return "30-90d"
    return ">90d"


def _price_bucket(p: float) -> str:
    if p < 0.2:
        return "0.05-0.2"
    if p < 0.4:
        return "0.2-0.4"
    if p < 0.6:
        return "0.4-0.6"
    if p < 0.8:
        return "0.6-0.8"
    return "0.8-0.95"


def _collect(n: int) -> list[Row]:
    rows: list[Row] = []
    seen: set[str] = set()
    for ev in _events(pages=5):
        cat = _event_category(ev)
        for m in ev.get("markets") or []:
            cid = m.get("conditionId")
            if not cid or cid in seen:
                continue
            tp = _token_and_price(m)
            d = _days(m)
            if tp is None or d is None or d < 1 or not (_MID_LO < tp[1] < _MID_HI):
                continue
            seen.add(cid)
            drift = _drift_1h(tp[0])
            if drift is None:
                continue
            vol = float(m.get("volume24hr") or 0.0)
            rows.append(Row(cat, tp[1], d, vol, _net_edge(cat, tp[1], d), drift))
            if len(rows) >= n:
                return rows
    return rows


def _segment(rows: list[Row], key, order=None) -> None:
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    print(f"  {'segment':12} {'n':>4} {'edge¢':>7} {'drift¢':>7} "
          f"{'ratio':>7} {'%works':>7} {'med_vol':>10}")
    items = sorted(groups.items(), key=lambda kv: -statistics.median(r.ratio for r in kv[1]))
    if order:
        items = sorted(groups.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
    for name, rs in items:
        if len(rs) < 2:
            continue
        works = sum(1 for r in rs if r.ratio >= 1.0 and r.drift > 0)
        print(f"  {name:12} {len(rs):>4} "
              f"{statistics.median(r.edge for r in rs) * 100:>7.3f} "
              f"{statistics.median(r.drift for r in rs) * 100:>7.3f} "
              f"{statistics.median(r.ratio for r in rs):>7.2f} "
              f"{100 * works / len(rs):>6.0f}% "
              f"{statistics.median(r.vol for r in rs):>10,.0f}")


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    print(f"Collecting up to {n} mid-range markets across categories...\n")
    rows = _collect(n)
    if len(rows) < 5:
        print("Not enough markets with usable history.")
        return 1
    print(f"Analyzed {len(rows)} markets. edge=half_spread-fee+rebate; "
          f"drift=hourly move; ratio=edge/drift; works = ratio>=1.\n")

    print("By CATEGORY (best segments first):")
    _segment(rows, lambda r: r.cat)
    print("\nBy TIME TO RESOLUTION:")
    _segment(rows, lambda r: _days_bucket(r.days), order=["<7d", "7-30d", "30-90d", ">90d"])
    print("\nBy PRICE LEVEL:")
    _segment(rows, lambda r: _price_bucket(r.p),
             order=["0.05-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-0.95"])

    top = sorted(rows, key=lambda r: -r.ratio)[:12]
    print("\nTop markets by edge-to-noise:")
    print(f"  {'ratio':>7} {'edge¢':>7} {'drift¢':>7} {'p':>5} {'days':>6}  category")
    for r in top:
        rr = f"{r.ratio:>7.1f}" if r.ratio != float("inf") else "    inf"
        print(f"  {rr} {r.edge*100:>7.3f} {r.drift*100:>7.3f} {r.p:>5.2f} {r.days:>6.0f}  {r.cat}")

    active = [r for r in rows if r.drift > 0]
    print("\n" + "=" * 60)
    print(f" Overall: {sum(1 for r in rows if r.ratio>=1 and r.drift>0)}/{len(rows)} markets "
          f"have edge >= drift. Of markets that actually move ({len(active)}),")
    if active:
        print(f" median edge-to-noise = {statistics.median(r.ratio for r in active):.2f}x.")
    print(" The strategy fits CALM, longer-dated markets; it gets picked off on")
    print(" fast news/short-dated ones. Use the segment tables to pick a universe.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
