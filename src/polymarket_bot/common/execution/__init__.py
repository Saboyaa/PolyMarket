"""Execution layer: the Executor contract plus paper/live implementations.

``base`` is frozen in the shared foundation because both the paper executor
(Phase 1 strategy branch) and the live executor (I/O branch) implement it.
"""

from polymarket_bot.common.execution.base import ExecutionResult, Executor
from polymarket_bot.common.execution.live import LiveExecutionError, LiveExecutor
from polymarket_bot.common.execution.maker_base import MakerExecutor
from polymarket_bot.common.execution.paper import PaperExecutor
from polymarket_bot.common.execution.paper_maker import PaperMakerExecutor

__all__ = [
    "Executor",
    "ExecutionResult",
    "PaperExecutor",
    "LiveExecutor",
    "LiveExecutionError",
    "MakerExecutor",
    "PaperMakerExecutor",
]
