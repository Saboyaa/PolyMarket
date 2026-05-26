"""A rolling JSONL log of market-making quotes, inventory, and PnL.

Each scan records one row per market: the book mid, the quote we posted (or that
a stop pulled it), and the executor's running inventory and PnL. The file is
capped at ``max_records`` lines (oldest trimmed) so it stays analysis-friendly.
All money fields are written as strings to preserve ``Decimal`` precision.

Analyze with ``scripts/analyze_mm_log.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from polymarket_bot.common.models import InventoryState, Market, OrderBook, Quote, Side

logger = logging.getLogger(__name__)


def _s(value: object) -> str | None:
    return None if value is None else str(value)


class QuoteLog:
    """Append-only JSONL sink of quote/inventory snapshots with a rolling cap."""

    def __init__(self, path: str | Path, *, max_records: int = 2000, clock=None) -> None:
        self._path = Path(path)
        self._max = max_records
        self._clock = clock or (lambda: datetime.now(UTC))
        self._trim_every = max(self._max // 4, 50)
        self._since_trim = 0

    def record(
        self,
        market: Market,
        book: OrderBook,
        quote: Quote | None,
        inventory: InventoryState,
    ) -> None:
        mid = book.mid_price(Side.YES)
        row = {
            "ts": self._clock().isoformat(),
            "condition_id": market.condition_id,
            "category": market.category,
            "mid": _s(mid),
            "quoted": quote is not None,
            "bid": _s(quote.bid) if quote else None,
            "ask": _s(quote.ask) if quote else None,
            "bid_size": _s(quote.bid_size) if quote else None,
            "ask_size": _s(quote.ask_size) if quote else None,
            "half_spread": _s(quote.half_spread) if quote else None,
            "net_yes": _s(inventory.net_yes),
            "net_pnl": _s(inventory.net_pnl),
            "fees_paid": _s(inventory.fees_paid),
            "rebates_earned": _s(inventory.rebates_earned),
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        self._since_trim += 1
        if self._since_trim >= self._trim_every:
            self._trim()

    def _trim(self) -> None:
        self._since_trim = 0
        if not self._path.exists():
            return
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if len(lines) > self._max:
            self._path.write_text("\n".join(lines[-self._max :]) + "\n", encoding="utf-8")
            logger.debug("Trimmed MM quote log to last %d records", self._max)
