"""Market discovery via Polymarket's Gamma REST API.

Gamma is the public, read-only data API used to enumerate markets. We discover
through the ``/events`` endpoint rather than ``/markets`` because that is where
categorization lives: the market records themselves return ``category: null``,
but the parent *event* carries ``tags`` (e.g. ``Politics``, ``Crypto``) we map
to a market category. We then apply the configured :class:`MarketSelector`
(condition-id allowlist and/or category filter). An empty selector returns all
active markets.

All HTTP goes through ``httpx``; default tests inject a fake transport / mock so
no real network calls happen.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import httpx

from polymarket_bot.common.config import MarketSelector
from polymarket_bot.common.models import Market

logger = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"
_PAGE_LIMIT = 100
_MAX_RETRIES = 5
# Gamma rejects paging past this offset with HTTP 422; stop before we hit it.
_MAX_OFFSET = 10_000

# An event carries several tags, broad and narrow, in no fixed order (e.g.
# ``['France', 'Politics', 'Macron', 'World']``). Prefer a recognized top-level
# category so per-category stats stay coarse and comparable; fall back to the
# first tag, then "Other".
_CATEGORY_PRIORITY: tuple[str, ...] = (
    "Politics",
    "Sports",
    "Crypto",
    "Economy",
    "Business",
    "Finance",
    "Tech",
    "Science",
    "World",
    "Elections",
    "Pop Culture",
    "Culture",
)


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

    def _fetch_active_events(self) -> list[dict]:
        """Page through active, non-closed events up to the Gamma offset cap.

        Gamma rejects ``offset`` beyond :data:`_MAX_OFFSET` with HTTP 422, so we
        stop before requesting an out-of-range page; a 422 is also caught
        defensively (in case the cap shifts) and treated as end-of-results.
        """
        out: list[dict] = []
        offset = 0
        while offset <= _MAX_OFFSET:
            try:
                resp = self._get(
                    "/events",
                    {
                        "active": "true",
                        "closed": "false",
                        "limit": _PAGE_LIMIT,
                        "offset": offset,
                    },
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    logger.warning("Gamma offset %d out of range; stopping pagination", offset)
                    break
                raise
            page = resp.json()
            if not page:
                break
            out.extend(page)
            if len(page) < _PAGE_LIMIT:
                break
            offset += _PAGE_LIMIT
        return out

    def discover_markets(self, selector: MarketSelector | None = None) -> list[Market]:
        """Return active binary markets, filtered by ``selector`` if given.

        Markets are flattened out of their parent events, each inheriting the
        event's derived category. A condition id is emitted once even if it
        appears under more than one event.
        """
        selector = selector or MarketSelector()
        markets: list[Market] = []
        seen: set[str] = set()
        for event in self._fetch_active_events():
            category = _event_category(event)
            for raw in event.get("markets") or []:
                market = _to_market(raw, category=category)
                if market is None or market.condition_id in seen:
                    continue
                seen.add(market.condition_id)
                markets.append(market)
        if selector.selects_all:
            return markets
        return [m for m in markets if _matches(m, selector)]


def _matches(market: Market, selector: MarketSelector) -> bool:
    if selector.condition_ids and market.condition_id not in selector.condition_ids:
        return False
    if selector.categories and market.category not in selector.categories:
        return False
    return True


def _event_category(event: dict) -> str:
    """Derive a coarse category from an event's tags.

    Returns the first tag matching :data:`_CATEGORY_PRIORITY` (case-insensitive),
    else the first tag's label, else ``"Other"``.
    """
    labels = [str(t.get("label", "")).strip() for t in (event.get("tags") or [])]
    labels = [label for label in labels if label]
    if not labels:
        return "Other"
    lowered = {label.lower(): label for label in labels}
    for preferred in _CATEGORY_PRIORITY:
        if preferred.lower() in lowered:
            return preferred
    return labels[0]


def _to_market(raw: dict, *, category: str | None = None) -> Market | None:
    """Map a Gamma market record to our :class:`Market`, or None if not binary.

    ``category`` overrides the (typically null) per-market category with one
    derived from the parent event; it falls back to the record's own field.
    """
    condition_id = raw.get("conditionId") or raw.get("condition_id")
    token_ids = _parse_token_ids(raw)
    if not condition_id or len(token_ids) != 2:
        return None
    resolved_category = category or str(raw.get("category", "") or "Other")
    return Market(
        condition_id=str(condition_id),
        question=str(raw.get("question", "")),
        category=resolved_category,
        yes_token_id=str(token_ids[0]),
        no_token_id=str(token_ids[1]),
        active=bool(raw.get("active", True)),
        end_date=_parse_end_date(raw.get("endDate") or raw.get("end_date")),
    )


def _parse_end_date(raw: object) -> datetime | None:
    """Parse Gamma's ISO ``endDate`` (e.g. ``2025-12-31T12:00:00Z``) to UTC.

    Returns ``None`` for missing or unparseable values; a naive datetime is
    assumed to be UTC. Phase 2 needs this to compute time-to-resolution.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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
