"""Command-line entry point for the Phase 1 arbitrage bot.

Paper is the default and needs no wallet. Live trading is gated behind a
**double guard** — both ``--mode live`` *and* ``--i-understand-the-risks`` must
be present — plus an interactive human confirmation (the go-live gate, T13).

    python -m polymarket_bot.phase1_arbitrage.cli --once
    python -m polymarket_bot.phase1_arbitrage.cli --config config.toml --max-scans 10
    python -m polymarket_bot.phase1_arbitrage.cli --mode live --i-understand-the-risks
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution.base import Executor
from polymarket_bot.common.execution.live import LiveExecutor
from polymarket_bot.common.execution.paper import PaperExecutor
from polymarket_bot.common.fees import FeeSchedule, leg_fee, resolve_fee_rate
from polymarket_bot.common.models import Market, OrderBook
from polymarket_bot.common.observation_log import ObservationLog
from polymarket_bot.phase1_arbitrage.runner import ArbRunner

logger = logging.getLogger(__name__)


class GoLiveError(RuntimeError):
    """Raised when the live-trading gate is not satisfied."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polymarket-arb",
        description="Educational intra-market (YES+NO) arbitrage bot for Polymarket.",
    )
    p.add_argument(
        "--mode",
        choices=["paper", "live"],
        help="Override the config mode. 'paper' (default) simulates fills with no wallet.",
    )
    p.add_argument("--config", help="Path to a TOML config file (optional).")
    p.add_argument(
        "--i-understand-the-risks",
        action="store_true",
        help="Second guard required (with --mode live) to arm REAL trading.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan pass and exit.",
    )
    p.add_argument(
        "--max-scans",
        type=int,
        default=None,
        help="Stop after N scan passes (default: run until interrupted).",
    )
    return p


def go_live_gate(
    config: Config,
    i_understand_the_risks: bool,
    *,
    confirm: Callable[[str], str] = input,
) -> None:
    """T13 — the human go-live gate. Returns only if live trading is authorized.

    Enforces the double guard (``mode == 'live'`` AND ``i_understand_the_risks``)
    and then requires an explicit typed confirmation. Raises :class:`GoLiveError`
    on any failure. Never reached in paper mode (caller only invokes for live).
    """
    if not config.is_live:
        raise GoLiveError("go_live_gate called but mode is not 'live'")
    if not i_understand_the_risks:
        raise GoLiveError("Live trading requires BOTH --mode live AND --i-understand-the-risks.")

    print("\n" + "=" * 60)
    print(" LIVE TRADING — this will place REAL orders with REAL funds.")
    print(f"   max total exposure : {config.risk.max_total_exposure} USDC")
    print(f"   max trade notional : {config.risk.max_trade_notional} USDC")
    print(f"   min net edge/share : {config.risk.min_net_edge_per_share}")
    print("=" * 60)
    answer = confirm("Type 'I UNDERSTAND' to proceed, anything else to abort: ")
    if answer.strip() != "I UNDERSTAND":
        raise GoLiveError("Live trading not confirmed; aborting.")
    logger.warning("Live trading armed by explicit human confirmation.")


def _paper_factory(config: Config, fees: FeeSchedule) -> Callable[[Market, OrderBook], Executor]:
    def factory(_market: Market, book: OrderBook) -> Executor:
        return PaperExecutor(
            book=book,
            fees=fees,
            max_completion_slippage=config.risk.max_completion_slippage,
            as_of=date.today(),
        )

    return factory


def _live_factory(
    config: Config, fees: FeeSchedule, clob_client: object
) -> Callable[[Market, OrderBook], Executor]:
    # One long-lived executor; token map grows as markets are discovered.
    token_map: dict[str, tuple[str, str]] = {}

    def fee_fn(category: str, price: Decimal, shares: Decimal) -> Decimal:
        rate = resolve_fee_rate(category, fees, date.today(), config.fees)
        return leg_fee(shares, rate, price)

    executor = LiveExecutor(
        config=config,
        clob_client=clob_client,
        token_resolver=lambda cid: token_map[cid],
        fee_fn=fee_fn,
        i_understand_the_risks=True,  # gate already passed before we get here
    )

    def factory(market: Market, _book: OrderBook) -> Executor:
        token_map[market.condition_id] = (market.yes_token_id, market.no_token_id)
        return executor

    return factory


def build_observation_log(config: Config) -> ObservationLog | None:
    """Build the rolling scan log from ``config.log`` (None if disabled)."""
    if not config.log.enabled:
        return None
    return ObservationLog(
        config.log.path,
        max_records=config.log.max_records,
        near_miss_gap=config.log.near_miss_gap,
    )


def build_runner(
    config: Config,
    *,
    market_source: object | None = None,
    book_source: object | None = None,
    executor_factory: Callable[[Market, OrderBook], Executor] | None = None,
    fees: FeeSchedule | None = None,
    observation_log: ObservationLog | None = None,
) -> ArbRunner:
    """Wire a runner for ``config``. Sources/factory are injectable for tests.

    When not injected, real Gamma/CLOB clients are constructed lazily so that
    importing this module (and running paper unit tests) needs no network.
    """
    fees = fees or FeeSchedule.default()
    if observation_log is None:
        observation_log = build_observation_log(config)

    if market_source is None:
        from polymarket_bot.common.clients.gamma import GammaClient

        market_source = GammaClient()

    if book_source is None or (config.is_live and executor_factory is None):
        from polymarket_bot.common.auth import get_creds
        from polymarket_bot.common.clients.clob import ClobMarketClient
        from polymarket_bot.common.config import wallet_private_key

        if config.is_live:
            creds = get_creds()
            key = wallet_private_key()  # presence already enforced by get_creds
            assert key is not None
            authed = ClobMarketClient.from_creds(key, creds)
            book_source = book_source or authed
            if executor_factory is None:
                executor_factory = _live_factory(config, fees, authed)
        else:
            # Paper: public read-only CLOB client (no wallet, no auth).
            from py_clob_client.client import ClobClient

            from polymarket_bot.common.auth import CHAIN_ID, CLOB_HOST

            book_source = book_source or ClobMarketClient(ClobClient(CLOB_HOST, chain_id=CHAIN_ID))

    if executor_factory is None:
        executor_factory = _paper_factory(config, fees)

    return ArbRunner(
        config,
        market_source,
        book_source,
        executor_factory,
        fees=fees,
        observation_log=observation_log,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    config = Config.load(args.config) if args.config else Config()
    if args.mode is not None:
        config = config.model_copy(update={"mode": args.mode})

    if config.is_live:
        go_live_gate(config, args.i_understand_the_risks)
    else:
        logger.info("Running in PAPER mode — no real orders, no wallet required.")

    runner = build_runner(config)
    max_scans = 1 if args.once else args.max_scans
    runner.run(max_scans=max_scans)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
