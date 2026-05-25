"""Execution layer: the Executor contract plus paper/live implementations.

``base`` is frozen in the shared foundation because both the paper executor
(Phase 1 strategy branch) and the live executor (I/O branch) implement it.
"""

from polymarket_bot.common.execution.base import ExecutionResult, Executor

__all__ = ["Executor", "ExecutionResult"]
