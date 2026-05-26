"""The Phase 1 scan loop: discover → fetch book → detect → size → execute.

The runner is execution-agnostic. It takes an *executor factory* so the same
loop drives both paper and live runs:

* paper — :class:`~common.execution.paper.PaperExecutor` is book-bound, so the
  factory builds a fresh one per market from the freshly fetched book;
* live — :class:`~common.execution.live.LiveExecutor` is long-lived and fetches
  its own books, so the factory returns the one shared instance.

Because per-scan paper executors reset their own ``open_exposure`` to zero, the
runner — not the executor — is the authority on *cumulative* exposure, so the
total-exposure cap holds across the whole run, not just within one scan.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution.base import ExecutionResult, Executor
from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import Market, OrderBook
from polymarket_bot.common.observation_log import Observation, ObservationLog
from polymarket_bot.common.risk import apply_caps
from polymarket_bot.phase1_arbitrage.strategy import evaluate_book, find_intramarket_arb

logger = logging.getLogger(__name__)

# ``(market, book) -> Executor``. The ``book`` arg lets a paper factory bind the
# executor to the freshly fetched book; a live factory may ignore it.
ExecutorFactory = Callable[[Market, OrderBook], Executor]


# Anything exposing ``get_order_book(condition_id, yes_token_id, no_token_id)``.
class _BookSource:  # pragma: no cover - structural type, documentation only
    def get_order_book(
        self, condition_id: str, yes_token_id: str, no_token_id: str
    ) -> OrderBook: ...


# Anything exposing ``discover_markets(selector) -> list[Market]``.
class _MarketSource:  # pragma: no cover - structural type, documentation only
    def discover_markets(self, selector: object) -> list[Market]: ...


class ArbRunner:
    """Drives repeated arbitrage scans across the selected markets."""

    def __init__(
        self,
        config: Config,
        market_source: _MarketSource,
        book_source: _BookSource,
        executor_factory: ExecutorFactory,
        *,
        fees: FeeSchedule | None = None,
        observation_log: ObservationLog | None = None,
        clock: Callable[[], date] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._markets = market_source
        self._books = book_source
        self._make_executor = executor_factory
        self._fees = fees or FeeSchedule.default()
        self._log = observation_log
        self._clock = clock or date.today
        self._sleep = sleep
        self._total_exposure = Decimal(0)

    @property
    def total_exposure(self) -> Decimal:
        """Cumulative USDC committed to completed pairs so far this run."""
        return self._total_exposure

    def scan_once(self) -> list[ExecutionResult]:
        """One full pass over the selected markets. Returns what executed."""
        as_of = self._clock()
        results: list[ExecutionResult] = []

        for market in self._markets.discover_markets(self._config.market_selector):
            if not market.active:
                continue
            result = self._scan_market(market, as_of)
            if result is not None:
                results.append(result)
        return results

    def _scan_market(self, market: Market, as_of: date) -> ExecutionResult | None:
        try:
            book = self._books.get_order_book(
                market.condition_id, market.yes_token_id, market.no_token_id
            )
        except Exception:  # noqa: BLE001 - one bad market must not kill the loop
            logger.exception("failed to fetch book for %s", market.condition_id)
            return None

        # Price both legs once; the log and the action decision share these numbers.
        ev = evaluate_book(book, self._fees, market.category, as_of, self._config.fees)
        if ev is None:
            return None  # missing a side; nothing to observe or act on

        opp = find_intramarket_arb(
            book,
            self._fees,
            self._config.risk.min_net_edge_per_share,
            market.category,
            as_of,
            self._config.fees,
        )

        result: ExecutionResult | None = None
        # Act only when actionable AND there is exposure headroom left.
        capped = self._total_exposure >= self._config.risk.max_total_exposure
        if opp is not None and not capped:
            sized = apply_caps(opp, self._total_exposure, self._config.risk)
            if sized is not None:
                executor = self._make_executor(market, book)
                result = executor.execute(sized)
                if result.completed:
                    self._total_exposure += sized.notional
                    logger.info(
                        "executed %s: size=%s edge/share=%s total_exposure=%s",
                        market.condition_id,
                        sized.size,
                        result.realized_edge,
                        self._total_exposure,
                    )
                else:
                    logger.warning("did not complete %s: %s", market.condition_id, result.note)

        self._observe(market, ev, actionable=opp is not None, result=result)
        return result

    def _observe(self, market, ev, *, actionable: bool, result: ExecutionResult | None) -> None:
        if self._log is None:
            return
        executed = bool(result and result.completed)
        self._log.record(
            Observation(
                condition_id=market.condition_id,
                category=market.category,
                yes_ask=ev.yes_ask,
                no_ask=ev.no_ask,
                size=ev.size,
                gross_edge_per_share=ev.gross_edge_per_share,
                net_edge_per_share=ev.net_edge_per_share,
                actionable=actionable,
                executed=executed,
                realized_edge=result.realized_edge if result else None,
            )
        )

    def run(self, max_scans: int | None = None) -> None:
        """Scan repeatedly every ``scan_interval`` seconds.

        ``max_scans`` bounds the number of passes (``None`` = run forever until
        interrupted). Sleeps between passes but not after the final one.
        """
        scan = 0
        while max_scans is None or scan < max_scans:
            scan += 1
            try:
                self.scan_once()
            except KeyboardInterrupt:
                logger.info("interrupted; stopping after %d scan(s)", scan)
                return
            if max_scans is not None and scan >= max_scans:
                break
            self._sleep(self._config.scan_interval)
