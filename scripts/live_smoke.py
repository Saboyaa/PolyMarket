"""Bounded live-data smoke test (paper mode, no wallet).

Samples active Polymarket markets, fetches their real CLOB order books, and:

1. ranks markets by how close ``YES_ask + NO_ask`` gets to $1 — so you can *see*
   the detector working (near-misses) even when nothing is actionable;
2. runs a real arbitrage scan through the paper pipeline (simulated fills).

Read-only; places no orders. A sum below $1.00 is a *gross* arb; it is only
*actionable* if the net edge after fees still clears ``min_net_edge_per_share``.

    uv run python scripts/live_smoke.py [N]   # N = markets to sample (default 40)
"""

from __future__ import annotations

import sys
from decimal import Decimal

import httpx

from polymarket_bot.common.clients.clob import ClobMarketClient
from polymarket_bot.common.clients.gamma import GAMMA_HOST, _to_market
from polymarket_bot.common.config import Config
from polymarket_bot.common.models import Market, Side
from polymarket_bot.phase1_arbitrage.cli import build_runner


class _BoundedMarkets:
    """A market source that returns a fixed, already-fetched sample."""

    def __init__(self, markets: list[Market]) -> None:
        self._markets = markets

    def discover_markets(self, _selector: object) -> list[Market]:
        return self._markets


def _fetch_markets(limit: int) -> list[Market]:
    with httpx.Client(base_url=GAMMA_HOST, timeout=15.0) as c:
        resp = c.get(
            "/markets",
            params={"active": "true", "closed": "false", "limit": min(limit, 100), "offset": 0},
        )
        resp.raise_for_status()
        raw_page = resp.json()
    return [m for m in (_to_market(r) for r in raw_page) if m and m.active][:limit]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    markets = _fetch_markets(limit=n)
    print(f"Sampled {len(markets)} live binary markets.\n")

    cfg = Config()  # paper, $10 cap, default fees
    from py_clob_client.client import ClobClient

    from polymarket_bot.common.auth import CHAIN_ID, CLOB_HOST

    book_source = ClobMarketClient(ClobClient(CLOB_HOST, chain_id=CHAIN_ID))

    # --- Diagnostic: rank by YES+NO sum so detection is visible -------------
    rows: list[tuple[Decimal, Market]] = []
    for m in markets:
        try:
            b = book_source.get_order_book(m.condition_id, m.yes_token_id, m.no_token_id)
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            print(f"  ! book fetch failed for {m.condition_id[:12]}…: {exc}")
            continue
        ya, na = b.best_ask(Side.YES), b.best_ask(Side.NO)
        if ya is None or na is None:
            continue
        rows.append((ya.price + na.price, m))

    rows.sort(key=lambda r: r[0])
    print("Closest markets to an arbitrage (YES_ask + NO_ask, lowest first):")
    print(f"  {'sum':>7}  {'gap<$1':>8}  market")
    for total, m in rows[:15]:
        gap = Decimal(1) - total
        flag = "  <-- GROSS ARB" if total < 1 else ""
        print(f"  {total:>7}  {gap:>8}  {m.question[:46]}{flag}")

    # --- Full pipeline: detect -> size -> paper-execute (with scan log) -----
    from polymarket_bot.phase1_arbitrage.cli import build_observation_log

    obs_log = build_observation_log(cfg)
    runner = build_runner(
        cfg,
        market_source=_BoundedMarkets(markets),
        book_source=book_source,
        observation_log=obs_log,
    )
    print("\nRunning the strategy + paper executor over the sample…")
    results = runner.scan_once()

    if not results:
        print(
            "\nNo ACTIONABLE arbitrage: either no sum dropped below $1, or the "
            f"net edge after fees did not clear min_net_edge_per_share="
            f"{cfg.risk.min_net_edge_per_share}."
        )
    else:
        print(f"\n{len(results)} opportunity/opportunities executed (simulated):")
        for r in results:
            o = r.opportunity
            print(
                f"  {o.condition_id[:12]}… ({o.category}) size={o.size} "
                f"yes={o.yes_ask} no={o.no_ask} net_edge/share={r.realized_edge} "
                f"completed={r.completed} fees={r.total_fees}"
            )
    s = runner.stats
    print("\n--- Scenario summary ------------------------------------------")
    print(f"  analyzed markets   : {s['analyzed']}")
    print(f"  gross arbs (<$1)   : {s['gross_arbs']}  ({s['gross_arb_pct']}% hit)")
    print(f"  actionable (>fees) : {s['actionable']}  ({s['actionable_pct']}% hit)")
    print(f"  executed (paper)   : {s['executed']}  ({s['executed_pct']}% hit)")
    print("---------------------------------------------------------------")
    print(f"Total simulated exposure committed: {runner.total_exposure} USDC")
    if obs_log is not None:
        from pathlib import Path

        logged = Path(cfg.log.path)
        n = len(logged.read_text().splitlines()) if logged.exists() else 0
        print(
            f"Scan log: {n} near-miss/arb rows in {cfg.log.path} "
            f"(analyze with: uv run python scripts/analyze_log.py)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
