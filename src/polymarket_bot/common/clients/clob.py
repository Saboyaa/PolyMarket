"""Thin wrapper over ``py-clob-client`` for order books and order placement.

This isolates all CLOB I/O behind a small, typed surface that returns our own
foundation value objects (:class:`OrderBook` with ``Decimal`` prices/sizes).
Default tests inject a mocked ``ClobClient`` so no network calls occur.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException
from py_clob_client.order_builder.constants import BUY, SELL

from polymarket_bot.common.auth import CHAIN_ID, CLOB_HOST, ClobCreds
from polymarket_bot.common.models import Level, Order, OrderBook, Side

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5


def _sort_levels(levels: list[Level], *, ascending: bool) -> tuple[Level, ...]:
    return tuple(sorted(levels, key=lambda lv: lv.price, reverse=not ascending))


class ClobMarketClient:
    """Wraps a ``py-clob-client`` ClobClient with our value objects.

    Pass an already-constructed ``client`` (e.g. a mock) to avoid network/signing
    in tests. Otherwise call :meth:`from_creds` to build a real L2 client.
    """

    def __init__(self, client: object, max_retries: int = _MAX_RETRIES) -> None:
        self._client = client
        self._max_retries = max_retries

    @classmethod
    def from_creds(
        cls,
        private_key: str,
        creds: ClobCreds,
        max_retries: int = _MAX_RETRIES,
    ) -> ClobMarketClient:
        """Build a real, L2-authenticated client (performs no network on init)."""
        from py_clob_client.client import ClobClient

        api_creds = ApiCreds(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            api_passphrase=creds.api_passphrase,
        )
        client = ClobClient(
            CLOB_HOST,
            chain_id=CHAIN_ID,
            key=private_key,
            creds=api_creds,
        )
        return cls(client, max_retries=max_retries)

    def _with_backoff(self, fn, *args, **kwargs):
        """Call ``fn`` retrying on rate-limit (HTTP 429) with backoff."""
        backoff = 0.5
        for attempt in range(self._max_retries):
            try:
                return fn(*args, **kwargs)
            except PolyApiException as exc:
                if getattr(exc, "status_code", None) != 429:
                    raise
                if attempt == self._max_retries - 1:
                    raise
                logger.warning("CLOB rate-limited; backing off %.1fs", backoff)
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError("unreachable")  # pragma: no cover

    def get_order_book(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> OrderBook:
        """Fetch and merge YES/NO books into our :class:`OrderBook`."""
        yes_raw = self._with_backoff(self._client.get_order_book, yes_token_id)
        no_raw = self._with_backoff(self._client.get_order_book, no_token_id)
        return OrderBook(
            condition_id=condition_id,
            yes_asks=_levels(getattr(yes_raw, "asks", None), ascending=True),
            no_asks=_levels(getattr(no_raw, "asks", None), ascending=True),
            yes_bids=_levels(getattr(yes_raw, "bids", None), ascending=False),
            no_bids=_levels(getattr(no_raw, "bids", None), ascending=False),
        )

    def place_order(
        self,
        order: Order,
        token_id: str,
        order_type: str = OrderType.FOK,
    ) -> dict:
        """Create, sign, and post a single limit order. Returns the raw response.

        ``token_id`` is the CLOB token for ``order.side`` (YES or NO). All orders
        here are BUYs (the arb opens both legs by buying); SELL is supported for
        completeness / unwinding.
        """
        side = BUY if order.side in (Side.YES, Side.NO) else SELL
        args = OrderArgs(
            token_id=token_id,
            price=float(order.price),
            size=float(order.size),
            side=side,
        )
        signed = self._client.create_order(args)
        return self._with_backoff(self._client.post_order, signed, order_type)


def _levels(raw_levels: list | None, *, ascending: bool) -> tuple[Level, ...]:
    """Convert raw OrderSummary entries (str price/size) into Decimal Levels."""
    if not raw_levels:
        return ()
    levels = [
        Level(price=Decimal(str(lv.price)), size=Decimal(str(lv.size)))
        for lv in raw_levels
        if Decimal(str(lv.size)) > 0
    ]
    return _sort_levels(levels, ascending=ascending)
