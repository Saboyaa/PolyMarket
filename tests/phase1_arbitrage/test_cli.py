from decimal import Decimal

import pytest

from polymarket_bot.common.config import Config, RiskConfig
from polymarket_bot.common.execution.base import ExecutionResult, Executor
from polymarket_bot.common.models import Level, Market, Opportunity, OrderBook
from polymarket_bot.phase1_arbitrage import cli
from polymarket_bot.phase1_arbitrage.cli import GoLiveError, build_parser, go_live_gate

# ---- argument parsing -------------------------------------------------------


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.mode is None
    assert args.i_understand_the_risks is False
    assert args.once is False
    assert args.max_scans is None


def test_parser_live_flags():
    args = build_parser().parse_args(["--mode", "live", "--i-understand-the-risks"])
    assert args.mode == "live"
    assert args.i_understand_the_risks is True


# ---- go-live gate (T13) -----------------------------------------------------


def _live_cfg() -> Config:
    return Config(mode="live", risk=RiskConfig())


def test_gate_rejects_paper_mode():
    with pytest.raises(GoLiveError):
        go_live_gate(Config(mode="paper"), True)


def test_gate_requires_second_guard():
    with pytest.raises(GoLiveError, match="BOTH"):
        go_live_gate(_live_cfg(), False)


def test_gate_aborts_without_exact_confirmation():
    with pytest.raises(GoLiveError, match="not confirmed"):
        go_live_gate(_live_cfg(), True, confirm=lambda _p: "yes")


def test_gate_passes_with_exact_confirmation():
    # Returns None (no raise) when both guards + typed confirmation are present.
    assert go_live_gate(_live_cfg(), True, confirm=lambda _p: "I UNDERSTAND") is None


# ---- build_runner + main with injected fakes (no network) -------------------


class _FakeMarkets:
    def discover_markets(self, _selector):
        return [Market("c1", "Q?", "Politics", "c1-yes", "c1-no")]


class _FakeBooks:
    def get_order_book(self, _cid, _y, _n):
        return OrderBook(
            condition_id="c1",
            yes_asks=(Level(Decimal("0.40"), Decimal("100")),),
            no_asks=(Level(Decimal("0.55"), Decimal("100")),),
        )


class _RecordingExecutor(Executor):
    calls: list[Opportunity] = []

    def execute(self, opportunity: Opportunity) -> ExecutionResult:
        _RecordingExecutor.calls.append(opportunity)
        return ExecutionResult(opportunity, (), True, opportunity.net_edge_per_share, "ok")

    @property
    def open_exposure(self) -> Decimal:
        return Decimal(0)


def test_build_runner_paper_uses_injected_sources():
    runner = cli.build_runner(
        Config(),
        market_source=_FakeMarkets(),
        book_source=_FakeBooks(),
        executor_factory=lambda _m, _b: _RecordingExecutor(),
    )
    results = runner.scan_once()
    assert len(results) == 1 and results[0].completed


def test_main_paper_once_runs_without_network(monkeypatch):
    _RecordingExecutor.calls = []
    real_build_runner = cli.build_runner

    def fake_build_runner(config, **_kw):
        return real_build_runner(
            config,
            market_source=_FakeMarkets(),
            book_source=_FakeBooks(),
            executor_factory=lambda _m, _b: _RecordingExecutor(),
        )

    monkeypatch.setattr(cli, "build_runner", fake_build_runner)
    rc = cli.main(["--once"])
    assert rc == 0
    assert len(_RecordingExecutor.calls) == 1


def test_main_live_without_second_guard_aborts(monkeypatch):
    # Should never construct a runner; gate raises first.
    monkeypatch.setattr(
        cli, "build_runner", lambda *a, **k: pytest.fail("runner built despite failed gate")
    )
    with pytest.raises(GoLiveError):
        cli.main(["--mode", "live"])
