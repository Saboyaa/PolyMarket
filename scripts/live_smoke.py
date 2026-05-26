"""Bounded live-data smoke test (paper mode, no wallet).

Fetches ONE page of active Polymarket markets, samples a handful, and runs a
single real arbitrage scan through the paper pipeline (real Gamma + real CLOB
order books, simulated fills). Read-only; places no orders.

    uv run python scripts/live_smoke.py [N]   # N = markets to sample (default 12)
"""

from __future__ import annotations

import sys

import httpx

from polymarket_bot.common.clients.clob import ClobMarketClient
from polymarket_bot.common.clients.gamma import GAMMA_HOST, _to_market
from polymarket_bot.common.config import Config
from polymarket_bot.common.models import Market
from polymarket_bot.phase1_arbitrage.cli import build_runner


class _BoundedMarkets:
    """A market source that returns a fixed, already-fetched sample."""

    def __init__(self, markets: list[Market]) -> None:
        self._markets = markets

    def discover_markets(self, _selector: object) -> list[Market]:
        return self._markets


def _fetch_one_page(limit: int) -> list[Market]:
    with httpx.Client(base_url=GAMMA_HOST, timeout=15.0) as c:
        resp = c.get(
            "/markets",
            params={"active": "true", "closed": "false", "limit": limit, "offset": 0},
        )
        resp.raise_for_status()
        raw_page = resp.json()
    markets = [m for m in (_to_market(r) for r in raw_page) if m and m.active]
    return markets


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    markets = _fetch_one_page(limit=100)[:n]
    print(f"Sampled {len(markets)} live binary markets:")
    for m in markets:
        print(f"  {m.condition_id[:12]}… | {m.category:12.12} | {m.question[:48]}")

    cfg = Config()  # paper, $10 cap, default fees
    # Real public CLOB client for order books; paper executor simulates fills.
    from py_clob_client.client import ClobClient

    from polymarket_bot.common.auth import CHAIN_ID, CLOB_HOST

    book_source = ClobMarketClient(ClobClient(CLOB_HOST, chain_id=CHAIN_ID))

    runner = build_runner(
        cfg,
        market_source=_BoundedMarkets(markets),
        book_source=book_source,
    )

    print("\nScanning live order books (paper fills)…")
    results = runner.scan_once()

    if not results:
        print("\nNo executable arbitrage found across the sample (expected — "
              "real books rarely have YES+NO asks summing below $1 after fees).")
    else:
        print(f"\n{len(results)} opportunity/opportunities executed (simulated):")
        for r in results:
            o = r.opportunity
            print(
                f"  {o.condition_id[:12]}… size={o.size} "
                f"yes={o.yes_ask} no={o.no_ask} net_edge/share={r.realized_edge} "
                f"completed={r.completed} fees={r.total_fees}"
            )
    print(f"\nTotal simulated exposure committed: {runner.total_exposure} USDC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
