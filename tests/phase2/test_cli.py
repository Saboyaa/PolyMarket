from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from polymarket_bot.common.config import Config
from polymarket_bot.common.execution import PaperMakerExecutor
from polymarket_bot.common.fees import FeeSchedule
from polymarket_bot.common.models import Level, Market, OrderBook
from polymarket_bot.phase2_market_making import cli
from polymarket_bot.phase2_market_making.cli import GoLiveError, build_parser

_NOW = datetime(2026, 5, 26, tzinfo=UTC)


# ---- argument parsing -------------------------------------------------------


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.mode is None
    assert args.i_understand_the_risks is False
    assert args.once is False
    assert args.max_scans is None


def test_parser_live_flags():
    args = build_parser().parse_args(["--mode", "live", "--i-understand-the-risks"])
    assert args.mode == "live" and args.i_understand_the_risks is True


# ---- go-live gate is the reused Phase 1 gate --------------------------------


def test_reuses_phase1_gate():
    from polymarket_bot.phase1_arbitrage.cli import go_live_gate as p1_gate

    assert cli.go_live_gate is p1_gate


def test_gate_requires_second_guard():
    with pytest.raises(GoLiveError, match="BOTH"):
        cli.go_live_gate(Config(mode="live"), False)


# ---- build_runner + main with injected fakes (no network) -------------------


class _FakeMarkets:
    def discover_markets(self, _selector):
        return [
            Market("c1", "Q?", "Politics", "c1-y", "c1-n", end_date=_NOW + timedelta(days=30))
        ]


class _FakeBooks:
    def get_order_book(self, _cid, _y, _n):
        return OrderBook(
            condition_id="c1",
            yes_asks=(Level(Decimal("0.52"), Decimal("100")),),
            yes_bids=(Level(Decimal("0.48"), Decimal("100")),),
        )


def _factory(market):
    return PaperMakerExecutor(
        market.condition_id, market.category, FeeSchedule.default(), _NOW.date()
    )


def test_build_runner_paper_uses_injected_sources():
    runner = cli.build_runner(
        Config(),
        market_source=_FakeMarkets(),
        book_source=_FakeBooks(),
        executor_factory=_factory,
    )
    runner._clock = lambda: _NOW
    runner.scan_once()
    assert runner.stats["scans"] == 1
    assert len(runner._executors["c1"].open_orders) >= 1  # placed a quote


def test_main_paper_once_runs_without_network(monkeypatch, tmp_path):
    real_build_runner = cli.build_runner

    def fake_build_runner(config, **_kw):
        r = real_build_runner(
            config,
            market_source=_FakeMarkets(),
            book_source=_FakeBooks(),
            executor_factory=_factory,
            quote_log=None,
        )
        r._clock = lambda: _NOW
        return r

    monkeypatch.setattr(cli, "build_runner", fake_build_runner)
    assert cli.main(["--once"]) == 0


def test_main_live_without_second_guard_aborts(monkeypatch):
    monkeypatch.setattr(
        cli, "build_runner", lambda *a, **k: pytest.fail("runner built despite failed gate")
    )
    with pytest.raises(GoLiveError):
        cli.main(["--mode", "live"])
