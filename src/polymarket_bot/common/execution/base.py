"""The Executor contract shared by paper and live execution.

An executor takes a sized :class:`Opportunity` and attempts to open *both* legs
(buy YES and buy NO). Because Polymarket has no native two-leg atomic order, an
executor must handle the case where one leg fills and the other does not — the
live implementation crosses the spread to complete the pair within a configured
slippage bound, and halts if it cannot.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from polymarket_bot.common.models import Fill, Opportunity


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of executing an opportunity."""

    opportunity: Opportunity
    fills: tuple[Fill, ...]
    completed: bool  # True if both legs ended fully hedged (paired)
    realized_edge: Decimal  # actual net edge per share after real fills + fees
    note: str = ""  # human-readable detail (e.g. why halted / how completed)

    @property
    def total_fees(self) -> Decimal:
        return sum((f.fee for f in self.fills), Decimal(0))


class Executor(ABC):
    """Executes opportunities. Implementations: PaperExecutor, LiveExecutor."""

    @abstractmethod
    def execute(self, opportunity: Opportunity) -> ExecutionResult:
        """Open both legs of ``opportunity``; return what actually happened."""

    @property
    @abstractmethod
    def open_exposure(self) -> Decimal:
        """Current cumulative USDC exposure across open positions.

        Read by ``common.risk`` to enforce the total-exposure cap.
        """
