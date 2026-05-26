"""The Phase 2 market-making scan loop.

Each scan, per selected market: fetch the book, reconcile any fills on resting
orders, compute time-to-resolution ``T`` and volatility ``sigma``, build a quote,
refresh resting orders (cancel old, place new), and log. Quoting stops (a ``None``
quote) cause all resting orders to be cancelled so we sit flat.

A :class:`~common.execution.maker_base.MakerExecutor` is long-lived per market
(it holds resting orders and inventory across scans), so the runner caches one
per condition id via the injected factory. The runner is the authority on
*cumulative* exposure across markets: once the summed open exposure reaches
``max_total_exposure`` it stops placing inventory-growing orders, but keeps
reconciling and cancelling so existing positions can wind down.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution.maker_base import MakerExecutor
from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import MakerOrder, Market, OrderBook, Side
from polymarket_bot.phase2_market_making.strategy import build_quotes
from polymarket_bot.phase2_market_making.volatility import sigma as resolve_sigma

logger = logging.getLogger(__name__)

# ``market -> MakerExecutor`` (long-lived; created once per condition id).
MakerExecutorFactory = Callable[[Market], MakerExecutor]


class MMRunner:
    """Drives repeated market-making scans across the selected markets."""

    def __init__(
        self,
        config: Config,
        market_source,
        book_source,
        executor_factory: MakerExecutorFactory,
        *,
        fees: FeeSchedule | None = None,
        quote_log=None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._markets = market_source
        self._books = book_source
        self._make_executor = executor_factory
        self._fees = fees or FeeSchedule.default()
        self._log = quote_log
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._executors: dict[str, MakerExecutor] = {}
        self._mid_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=config.mm.sigma_window)
        )
        # Running tallies for reporting.
        self._scans = 0
        self._quoted = 0  # markets that produced a live quote
        self._stopped = 0  # markets where a stop pulled quotes
        self._fills = 0

    @property
    def total_exposure(self) -> Decimal:
        """Summed open exposure across every market's executor."""
        return sum((ex.open_exposure for ex in self._executors.values()), Decimal(0))

    @property
    def stats(self) -> dict[str, object]:
        return {
            "scans": self._scans,
            "quoted": self._quoted,
            "stopped": self._stopped,
            "fills": self._fills,
            "total_exposure": self.total_exposure,
            "net_pnl": sum((ex.inventory.net_pnl for ex in self._executors.values()), Decimal(0)),
        }

    def _executor_for(self, market: Market) -> MakerExecutor:
        ex = self._executors.get(market.condition_id)
        if ex is None:
            ex = self._make_executor(market)
            self._executors[market.condition_id] = ex
        return ex

    def _hours_to_resolution(self, market: Market, now: datetime) -> float | None:
        if market.end_date is None:
            return None
        return (market.end_date - now).total_seconds() / 3600.0

    def scan_once(self) -> None:
        """One pass over the selected markets."""
        self._scans += 1
        now = self._clock()
        for market in self._markets.discover_markets(self._config.market_selector):
            if not market.active:
                continue
            self._scan_market(market, now)

    def _scan_market(self, market: Market, now: datetime) -> None:
        try:
            book = self._books.get_order_book(
                market.condition_id, market.yes_token_id, market.no_token_id
            )
        except Exception:  # noqa: BLE001 - one bad market must not kill the loop
            logger.exception("failed to fetch book for %s", market.condition_id)
            return

        executor = self._executor_for(market)

        # 1. Settle any fills on orders resting from the previous scan.
        fills = executor.reconcile(book)
        self._fills += len(fills)

        # 2. Decide a new quote.
        hours = self._hours_to_resolution(market, now)
        mid = book.mid_price(Side.YES)
        if mid is not None:
            self._mid_history[market.condition_id].append(float(mid))
        sigma = resolve_sigma(self._config.mm, list(self._mid_history[market.condition_id]))
        quote = build_quotes(book, hours, sigma, executor.inventory, self._config.mm)

        # 3. Refresh: cancel everything resting, then (maybe) place the new quote.
        for order in executor.open_orders:
            if order.order_id is not None:
                executor.cancel(order.order_id)

        if quote is None:
            self._stopped += 1
            self._observe(market, book, None, executor)
            return

        capped = self.total_exposure >= self._config.risk.max_total_exposure
        if not capped:
            if quote.bid_size > 0:
                executor.place(
                    MakerOrder(market.condition_id, Side.YES, buy=True, price=quote.bid,
                               size=quote.bid_size)
                )
            if quote.ask_size > 0:
                executor.place(
                    MakerOrder(market.condition_id, Side.YES, buy=False, price=quote.ask,
                               size=quote.ask_size)
                )
            self._quoted += 1

        self._observe(market, book, quote, executor)

    def _observe(self, market, book: OrderBook, quote, executor: MakerExecutor) -> None:
        if self._log is None:
            return
        self._log.record(market, book, quote, executor.inventory)

    def run(self, max_scans: int | None = None) -> None:
        """Scan repeatedly every ``scan_interval`` seconds (``None`` = forever)."""
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
