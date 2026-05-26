"""Bounded paper market-making run against LIVE books (no wallet, no orders).

Samples a few liquid, longer-dated, mid-range markets, then runs the real
``MMRunner`` in paper mode for a handful of scans against live CLOB order books.
The paper maker executor rests quotes and fills them only when the live book
trades through them — so over a short run fills are usually rare (that's honest:
MM fills take time). What this proves is the end-to-end live pipeline: real
books -> model quote -> resting orders -> reconcile -> inventory/PnL, with the
calibrated parameters and all four stops active.

    uv run python scripts/paper_run_mm.py [N] [SCANS]   # defaults: 12 markets, 3 scans
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from polymarket_bot.common.config import Config, MarketMakingConfig
from polymarket_bot.common.models import Market, Side
from polymarket_bot.phase2_market_making.cli import build_runner

GAMMA = "https://gamma-api.polymarket.com"
_MIN_DAYS, _MID_LO, _MID_HI = 14.0, 0.05, 0.95

# Calibrated parameters (scripts/calibrate_mm.py). tick_size=0.001 matches the
# grid these markets actually trade on; a 0.01 grid clamps quotes across the
# touch on low-priced markets (turning the maker into a taker).
_MM = MarketMakingConfig(
    sigma="6.7", base_spread="0.005", k_gamma="0.0008",
    gamma_ceiling="51", tick_size="0.001", quote_size="10",
    min_hours_to_resolution="6", estimate_sigma=True,
)


def _last_price(m: dict) -> float | None:
    """Best-effort mid from Gamma's lastTradePrice / outcomePrices."""
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


def _days_to_end(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - datetime.now(UTC)).total_seconds() / 86400.0


def _sample_markets(n: int) -> list[Market]:
    r = httpx.get(
        f"{GAMMA}/markets",
        params={"active": "true", "closed": "false", "order": "volume24hr",
                "ascending": "false", "limit": 200},
        timeout=20.0,
    )
    r.raise_for_status()
    out: list[Market] = []
    for m in r.json():
        ids = m.get("clobTokenIds")
        if isinstance(ids, str):
            ids = json.loads(ids)
        if not ids or len(ids) != 2:
            continue
        end_raw = m.get("endDate")
        days = _days_to_end(end_raw)
        if days is None or days < _MIN_DAYS:
            continue
        price = _last_price(m)
        if price is None or not (_MID_LO < price < _MID_HI):
            continue  # skip longshots/near-certain markets (bad MM targets)
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        out.append(Market(
            condition_id=str(m.get("conditionId")),
            question=str(m.get("question", "")),
            category="Other",
            yes_token_id=str(ids[0]),
            no_token_id=str(ids[1]),
            end_date=end,
        ))
        if len(out) >= n:
            break
    return out


class _Bounded:
    def __init__(self, markets):
        self._m = markets

    def discover_markets(self, _selector):
        return self._m


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    scans = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    markets = _sample_markets(n)
    print(
        f"Sampled {len(markets)} liquid mid-range markets "
        f"(>{_MIN_DAYS:.0f}d). Paper-quoting {scans} scans.\n"
    )

    cfg = Config(mm=_MM)  # paper mode (default)
    runner = build_runner(cfg, market_source=_Bounded(markets), quote_log=None)

    for i in range(1, scans + 1):
        runner.scan_once()
        s = runner.stats
        print(f"scan {i}: quoted={s['quoted']:>3} stopped={s['stopped']:>3} "
              f"fills={s['fills']:>2} exposure={s['total_exposure']} pnl={s['net_pnl']}")
        if i < scans:
            time.sleep(2)

    # Show a few live quotes vs the market mid so the model is visible.
    print("\nSample resting quotes (model vs live book):")
    print(f"  {'mid':>5} {'our_bid':>8} {'our_ask':>8} {'half':>6}  market")
    shown = 0
    for cid, ex in runner._executors.items():
        orders = ex.open_orders
        if not orders:
            continue
        market = next(m for m in markets if m.condition_id == cid)
        book = runner._books.get_order_book(cid, market.yes_token_id, market.no_token_id)
        mid = book.mid_price(Side.YES)
        bid = next((o.price for o in orders if o.buy), None)
        ask = next((o.price for o in orders if not o.buy), None)
        half = (ask - bid) / Decimal(2) if bid and ask else None
        print(f"  {mid} {str(bid):>8} {str(ask):>8} {str(half):>6}  {market.question[:44]}")
        shown += 1
        if shown >= 8:
            break
    if shown == 0:
        print("  (no resting quotes — all sampled markets hit a stop)")

    s = runner.stats
    print("\n--- Summary ---------------------------------------------------")
    print(f"  scans={s['scans']} markets={len(runner._executors)} "
          f"quoted={s['quoted']} stopped={s['stopped']} fills={s['fills']}")
    print(f"  total simulated exposure : {s['total_exposure']} USDC")
    print(f"  net PnL (paper)          : {s['net_pnl']} USDC")
    print("  (fills are rare over a short run — quoting/pipeline is the point here)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
