"""Live executor — places REAL CLOB orders. Guarded behind two flags.

Safety model (spec Success Criteria 6 & 9):

- ``LiveExecutor`` will only place real orders when **both** guards are set:
  ``config.mode == "live"`` AND the caller passes ``i_understand_the_risks=True``
  at construction. Either guard missing => :meth:`execute` refuses and places
  nothing.
- One-leg-fill handling: the two legs (buy YES, buy NO) are placed as immediate
  (FOK) taker orders. If the first leg fills and the second does not, we *cross
  the spread* to complete the pair — re-pricing the second leg up to the best
  available ask, accepting reduced edge, but only within
  ``risk.max_completion_slippage``. If even crossing cannot complete within that
  bound, we place nothing more and return ``completed=False`` with a halt note
  (the caller should alert + stop).

The CLOB client is always injected so tests can mock it; nothing here imports a
live client by default.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution.base import ExecutionResult, Executor
from polymarket_bot.common.models import Fill, Opportunity, Order, OrderBook, Side

# A fee function maps (category, price, shares) -> USDC fee for a single fill.
# Injected so this module does not hard-depend on common.fees (owned elsewhere);
# defaults to "trust the fee reported by the CLOB response, else zero".
FeeFn = Callable[[str, Decimal, Decimal], Decimal]

logger = logging.getLogger(__name__)


class LiveExecutionError(RuntimeError):
    """Raised on an unrecoverable live-execution failure."""


class LiveExecutor(Executor):
    """Places real orders via an injected CLOB client, behind a double guard."""

    def __init__(
        self,
        config: Config,
        clob_client,
        token_resolver,
        fee_fn: FeeFn | None = None,
        *,
        i_understand_the_risks: bool = False,
    ) -> None:
        """
        Args:
            config: must have ``mode == "live"`` to arm live trading.
            clob_client: object exposing ``place_order(order, token_id, ...)``
                and ``get_order_book(condition_id, yes_token_id, no_token_id)``
                (see :class:`~common.clients.clob.ClobMarketClient`).
            token_resolver: ``condition_id -> (yes_token_id, no_token_id)``.
            fee_fn: optional ``(category, price, shares) -> fee``; if omitted,
                the fee reported by the CLOB response is used, else zero.
            i_understand_the_risks: explicit second guard.
        """
        self._config = config
        self._clob = clob_client
        self._resolve_tokens = token_resolver
        self._fee_fn = fee_fn
        self._armed = bool(i_understand_the_risks)
        self._open_exposure = Decimal(0)

    @property
    def open_exposure(self) -> Decimal:
        return self._open_exposure

    @property
    def is_armed(self) -> bool:
        """True only when BOTH guards are satisfied."""
        return self._config.is_live and self._armed

    def execute(self, opportunity: Opportunity) -> ExecutionResult:
        if not self.is_armed:
            reason = self._disarmed_reason()
            logger.warning("LiveExecutor refused to trade: %s", reason)
            return ExecutionResult(
                opportunity=opportunity,
                fills=(),
                completed=False,
                realized_edge=Decimal(0),
                note=f"refused: {reason}",
            )

        yes_token, no_token = self._resolve_tokens(opportunity.condition_id)

        # Leg 1: buy YES at the detected ask.
        leg1_order = Order(
            condition_id=opportunity.condition_id,
            side=Side.YES,
            price=opportunity.yes_ask,
            size=opportunity.size,
        )
        leg1_fill = self._place(leg1_order, yes_token)
        if leg1_fill is None or leg1_fill.size <= 0:
            return ExecutionResult(
                opportunity=opportunity,
                fills=(),
                completed=False,
                realized_edge=Decimal(0),
                note="leg 1 (YES) did not fill; no exposure taken",
            )

        # Leg 2: buy NO at the detected ask for the size that leg 1 actually filled.
        leg2_order = Order(
            condition_id=opportunity.condition_id,
            side=Side.NO,
            price=opportunity.no_ask,
            size=leg1_fill.size,
        )
        leg2_fill = self._place(leg2_order, no_token)
        if leg2_fill is not None and leg2_fill.size >= leg1_fill.size:
            return self._result(opportunity, (leg1_fill, leg2_fill), completed=True)

        # Leg 2 short-filled: cross the spread to complete the remainder.
        return self._complete_leg2(opportunity, leg1_fill, leg2_fill, no_token)

    def _complete_leg2(
        self,
        opportunity: Opportunity,
        leg1_fill: Fill,
        leg2_partial: Fill | None,
        no_token: str,
    ) -> ExecutionResult:
        """Cross the spread on NO to hedge ``leg1_fill`` within slippage bound."""
        filled = leg2_partial.size if leg2_partial else Decimal(0)
        remaining = leg1_fill.size - filled
        max_price = opportunity.no_ask + self._config.risk.max_completion_slippage

        book = self._clob.get_order_book(
            opportunity.condition_id,
            *self._resolve_tokens(opportunity.condition_id),
        )
        best = self._best_completion_ask(book, max_price, remaining)
        if best is None:
            note = (
                "HALT: cannot complete NO leg within "
                f"max_completion_slippage={self._config.risk.max_completion_slippage}; "
                f"holding naked YES exposure of {remaining} shares"
            )
            logger.error(note)
            fills = (leg1_fill,) if leg2_partial is None else (leg1_fill, leg2_partial)
            return self._result(opportunity, fills, completed=False, note=note)

        completion_order = Order(
            condition_id=opportunity.condition_id,
            side=Side.NO,
            price=best,
            size=remaining,
        )
        completion_fill = self._place(completion_order, no_token)
        if completion_fill is None or completion_fill.size < remaining:
            note = "HALT: completion order did not fully fill; naked exposure remains"
            logger.error(note)
            fills = tuple(f for f in (leg1_fill, leg2_partial, completion_fill) if f is not None)
            return self._result(opportunity, fills, completed=False, note=note)

        fills = tuple(f for f in (leg1_fill, leg2_partial, completion_fill) if f is not None)
        note = f"completed by crossing spread on NO at {best} (detected ask {opportunity.no_ask})"
        return self._result(opportunity, fills, completed=True, note=note)

    def _best_completion_ask(
        self,
        book: OrderBook,
        max_price: Decimal,
        needed: Decimal,
    ) -> Decimal | None:
        """Lowest NO ask price that clears ``needed`` size within ``max_price``.

        Returns the worst price we'd pay (the level that fills the remainder), or
        None if depth within the slippage bound is insufficient.
        """
        cumulative = Decimal(0)
        for level in book.no_asks:
            if level.price > max_price:
                break
            cumulative += level.size
            if cumulative >= needed:
                return level.price
        return None

    def _place(self, order: Order, token_id: str) -> Fill | None:
        """Place one order and adapt the raw response into a :class:`Fill`."""
        resp = self._clob.place_order(order, token_id)
        fill = _response_to_fill(order, resp, self._fee_fn)
        if fill is not None and fill.size > 0:
            self._open_exposure += fill.size * fill.price + fill.fee
        return fill

    def _result(
        self,
        opportunity: Opportunity,
        fills: tuple[Fill, ...],
        *,
        completed: bool,
        note: str = "",
    ) -> ExecutionResult:
        realized = _realized_edge(fills) if completed and fills else Decimal(0)
        return ExecutionResult(
            opportunity=opportunity,
            fills=fills,
            completed=completed,
            realized_edge=realized,
            note=note,
        )

    def _disarmed_reason(self) -> str:
        if not self._config.is_live:
            return "config.mode is not 'live'"
        return "i_understand_the_risks flag not set"


def _response_to_fill(order: Order, resp: object, fee_fn: FeeFn | None) -> Fill | None:
    """Map a CLOB post-order response to a :class:`Fill`.

    Responses vary; we accept a dict with ``size``/``price`` (and optional
    ``fee``) or a ``status`` indicating no match. Unfilled => None.
    """
    if not isinstance(resp, dict):
        return None
    status = str(resp.get("status", "")).lower()
    if status in {"unmatched", "rejected", "cancelled", "canceled"}:
        return None

    size = Decimal(str(resp.get("size", resp.get("matchedAmount", order.size))))
    if size <= 0:
        return None
    price = Decimal(str(resp.get("price", order.price)))
    if "fee" in resp:
        fee = Decimal(str(resp["fee"]))
    elif fee_fn is not None:
        fee = fee_fn(str(resp.get("category", "")), price, size)
    else:
        fee = Decimal(0)
    return Fill(
        condition_id=order.condition_id,
        side=order.side,
        price=price,
        size=size,
        fee=fee,
    )


def _realized_edge(fills: tuple[Fill, ...]) -> Decimal:
    """Net edge per share = (1 - total cost per paired share) after fees.

    Uses the smaller of the YES/NO filled sizes as the paired quantity.
    """
    yes = sum((f.size for f in fills if f.side is Side.YES), Decimal(0))
    no = sum((f.size for f in fills if f.side is Side.NO), Decimal(0))
    paired = min(yes, no)
    if paired <= 0:
        return Decimal(0)
    cost = sum((f.size * f.price + f.fee for f in fills), Decimal(0))
    return (paired - cost) / paired
