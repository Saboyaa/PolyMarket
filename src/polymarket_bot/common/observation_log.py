"""A rolling JSONL log of arbitrage observations for later analysis.

Each scan can record one row per market that is *at or near* an arbitrage
(``YES_ask + NO_ask`` within ``near_miss_gap`` of $1). Rows far from parity are
dropped so the file stays small and analysis-friendly. The file is capped at
``max_records`` lines (oldest trimmed), so it never grows without bound.

Read it back with e.g. pandas::

    import pandas as pd
    df = pd.read_json("scan_log.jsonl", lines=True)

All money fields are written as strings to preserve ``Decimal`` precision.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Observation:
    """One market's pricing at a point in time."""

    condition_id: str
    category: str
    yes_ask: Decimal
    no_ask: Decimal
    size: Decimal
    gross_edge_per_share: Decimal
    net_edge_per_share: Decimal
    actionable: bool  # passed the strategy's profit gate
    executed: bool  # an order pair was (simulated or real) filled
    realized_edge: Decimal | None = None  # net edge per share actually achieved

    def to_record(self, ts: str) -> dict:
        return {
            "ts": ts,
            "condition_id": self.condition_id,
            "category": self.category,
            "yes_ask": str(self.yes_ask),
            "no_ask": str(self.no_ask),
            "ask_sum": str(self.yes_ask + self.no_ask),
            "size": str(self.size),
            "gross_edge_per_share": str(self.gross_edge_per_share),
            "net_edge_per_share": str(self.net_edge_per_share),
            "actionable": self.actionable,
            "executed": self.executed,
            "realized_edge": None if self.realized_edge is None else str(self.realized_edge),
        }


class ObservationLog:
    """Append-only JSONL sink with a near-miss filter and a rolling cap."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_records: int = 2000,
        near_miss_gap: Decimal = Decimal("0.01"),
        clock: callable = lambda: datetime.now(UTC),
    ) -> None:
        self._path = Path(path)
        self._max = max_records
        self._gap = near_miss_gap
        self._clock = clock
        # Trim in batches rather than every write; never let the file exceed ~25% over cap.
        self._trim_every = max(self._max // 4, 50)
        self._since_trim = 0

    def is_near_miss(self, observation: Observation) -> bool:
        """True if ``ask_sum`` is at or within ``near_miss_gap`` above $1.

        i.e. ``gross_edge_per_share >= -near_miss_gap`` (a sum of 1.008 with a
        0.01 gap qualifies; 1.05 does not).
        """
        return observation.gross_edge_per_share >= -self._gap

    def record(self, observation: Observation) -> bool:
        """Append ``observation`` if it qualifies. Returns whether it was kept."""
        if not self.is_near_miss(observation):
            return False
        line = json.dumps(observation.to_record(self._clock().isoformat()))
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._since_trim += 1
        if self._since_trim >= self._trim_every:
            self._trim()
        return True

    def _trim(self) -> None:
        """Keep only the most recent ``max_records`` lines."""
        self._since_trim = 0
        if not self._path.exists():
            return
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if len(lines) > self._max:
            kept = lines[-self._max :]
            self._path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            logger.debug("Trimmed scan log to last %d records", self._max)
