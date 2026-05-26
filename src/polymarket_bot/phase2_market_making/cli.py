"""Command-line entry point for the Phase 2 market-making bot.

Paper is the default and needs no wallet. Live trading is gated behind the same
**double guard** as Phase 1 — both ``--mode live`` *and* ``--i-understand-the-risks``
— plus the interactive human confirmation (the shared ``go_live_gate``).

    python -m polymarket_bot.phase2_market_making.cli --once
    python -m polymarket_bot.phase2_market_making.cli --config config.toml --max-scans 10
    python -m polymarket_bot.phase2_market_making.cli --mode live --i-understand-the-risks
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from datetime import date

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution.live_maker import LiveMakerExecutor
from polymarket_bot.common.execution.maker_base import MakerExecutor
from polymarket_bot.common.execution.paper_maker import PaperMakerExecutor
from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import Market
from polymarket_bot.phase1_arbitrage.cli import GoLiveError, go_live_gate  # reused verbatim
from polymarket_bot.phase2_market_making.quote_log import QuoteLog
from polymarket_bot.phase2_market_making.runner import MMRunner

logger = logging.getLogger(__name__)

_MM_LOG_PATH = "mm_log.jsonl"

__all__ = ["GoLiveError", "go_live_gate", "build_parser", "build_runner", "main"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polymarket-mm",
        description="Educational Black-Scholes binary market-making bot for Polymarket.",
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
    p.add_argument("--once", action="store_true", help="Run a single scan pass and exit.")
    p.add_argument(
        "--max-scans",
        type=int,
        default=None,
        help="Stop after N scan passes (default: run until interrupted).",
    )
    return p


def build_quote_log(config: Config) -> QuoteLog | None:
    """Build the rolling MM quote log (None if logging disabled in config)."""
    if not config.log.enabled:
        return None
    return QuoteLog(_MM_LOG_PATH, max_records=config.log.max_records)


def _paper_factory(fees: FeeSchedule) -> Callable[[Market], MakerExecutor]:
    def factory(market: Market) -> MakerExecutor:
        return PaperMakerExecutor(market.condition_id, market.category, fees, date.today())

    return factory


def _live_factory(
    config: Config, fees: FeeSchedule, clob_client: object
) -> Callable[[Market], MakerExecutor]:
    def factory(market: Market) -> MakerExecutor:
        tokens = (market.yes_token_id, market.no_token_id)
        return LiveMakerExecutor(
            config,
            clob_client,
            lambda _cid: tokens,
            market.condition_id,
            market.category,
            fees,
            date.today(),
            i_understand_the_risks=True,  # gate already passed before we get here
        )

    return factory


def build_runner(
    config: Config,
    *,
    market_source: object | None = None,
    book_source: object | None = None,
    executor_factory: Callable[[Market], MakerExecutor] | None = None,
    fees: FeeSchedule | None = None,
    quote_log: QuoteLog | None = None,
) -> MMRunner:
    """Wire an MM runner for ``config``. Sources/factory are injectable for tests.

    Real Gamma/CLOB clients are built lazily when not injected, so importing this
    module (and running paper unit tests) needs no network.
    """
    fees = fees or FeeSchedule.default()
    if quote_log is None:
        quote_log = build_quote_log(config)

    if market_source is None:
        from polymarket_bot.common.clients.gamma import GammaClient

        market_source = GammaClient()

    if book_source is None or (config.is_live and executor_factory is None):
        from polymarket_bot.common.clients.clob import ClobMarketClient

        if config.is_live:
            from polymarket_bot.common.auth import get_creds
            from polymarket_bot.common.config import wallet_private_key

            creds = get_creds()
            key = wallet_private_key()  # presence already enforced by get_creds
            assert key is not None
            authed = ClobMarketClient.from_creds(key, creds)
            book_source = book_source or authed
            if executor_factory is None:
                executor_factory = _live_factory(config, fees, authed)
        else:
            from py_clob_client.client import ClobClient

            from polymarket_bot.common.auth import CHAIN_ID, CLOB_HOST

            book_source = book_source or ClobMarketClient(ClobClient(CLOB_HOST, chain_id=CHAIN_ID))

    if executor_factory is None:
        executor_factory = _paper_factory(fees)

    return MMRunner(
        config,
        market_source,
        book_source,
        executor_factory,
        fees=fees,
        quote_log=quote_log,
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
