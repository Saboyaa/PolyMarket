"""Market discovery via Polymarket's Gamma REST API.

Gamma is the public, read-only data API used to enumerate markets. We use it to
discover active binary markets and apply the configured :class:`MarketSelector`
(condition-id allowlist and/or category filter). An empty selector returns all
active markets.

All HTTP goes through ``httpx``; default tests inject a fake transport / mock so
no real network calls happen.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from polymarket_bot.common.config import MarketSelector
from polymarket_bot.common.models import Market

logger = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"
_PAGE_LIMIT = 100
_MAX_RETRIES = 5


class GammaClient:
    """Thin wrapper over the Gamma markets REST endpoint."""

    def __init__(
        self,
        host: str = GAMMA_HOST,
        client: httpx.Client | None = None,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._host = host.rstrip("/")
        self._client = client or httpx.Client(base_url=self._host, timeout=10.0)
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GammaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, object]) -> httpx.Response:
        """GET with simple exponential backoff on HTTP 429 (rate limit)."""
        backoff = 0.5
        for attempt in range(self._max_retries):
            resp = self._client.get(path, params=params)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp
            if attempt == self._max_retries - 1:
                resp.raise_for_status()
            logger.warning("Gamma rate-limited; backing off %.1fs", backoff)
            time.sleep(backoff)
            backoff *= 2
        raise RuntimeError("unreachable")  # pragma: no cover

    def _fetch_active_markets(self) -> list[dict]:
        """Page through all active, non-closed markets."""
        out: list[dict] = []
        offset = 0
        while True:
            resp = self._get(
                "/markets",
                {
                    "active": "true",
                    "closed": "false",
                    "limit": _PAGE_LIMIT,
                    "offset": offset,
                },
            )
            page = resp.json()
            if not page:
                break
            out.extend(page)
            if len(page) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT
        return out

    def discover_markets(self, selector: MarketSelector | None = None) -> list[Market]:
        """Return active binary markets, filtered by ``selector`` if given."""
        selector = selector or MarketSelector()
        markets = [m for m in (_to_market(raw) for raw in self._fetch_active_markets()) if m]
        if selector.selects_all:
            return markets
        return [m for m in markets if _matches(m, selector)]


def _matches(market: Market, selector: MarketSelector) -> bool:
    if selector.condition_ids and market.condition_id not in selector.condition_ids:
        return False
    if selector.categories and market.category not in selector.categories:
        return False
    return True


def _to_market(raw: dict) -> Market | None:
    """Map a Gamma market record to our :class:`Market`, or None if not binary."""
    condition_id = raw.get("conditionId") or raw.get("condition_id")
    token_ids = _parse_token_ids(raw)
    if not condition_id or len(token_ids) != 2:
        return None
    return Market(
        condition_id=str(condition_id),
        question=str(raw.get("question", "")),
        category=str(raw.get("category", "") or "Other"),
        yes_token_id=str(token_ids[0]),
        no_token_id=str(token_ids[1]),
        active=bool(raw.get("active", True)),
    )


def _parse_token_ids(raw: dict) -> list[str]:
    """Gamma encodes ``clobTokenIds`` as a JSON-string list or a real list."""
    raw_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids")
    if raw_ids is None:
        return []
    if isinstance(raw_ids, str):
        try:
            raw_ids = json.loads(raw_ids)
        except json.JSONDecodeError:
            return []
    if isinstance(raw_ids, (list, tuple)):
        return [str(t) for t in raw_ids]
    return []
