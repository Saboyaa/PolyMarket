from decimal import Decimal

from polymarket_bot.common.config import Config, RiskConfig
from polymarket_bot.common.execution.base import ExecutionResult, Executor
from polymarket_bot.common.models import Level, Market, Opportunity, OrderBook
from polymarket_bot.phase1_arbitrage.runner import ArbRunner


def _market(cid: str, category: str = "Politics") -> Market:
    return Market(
        condition_id=cid,
        question=f"Q-{cid}?",
        category=category,
        yes_token_id=f"{cid}-yes",
        no_token_id=f"{cid}-no",
    )


def _arb_book(cid: str, yes: str, no: str, size: str = "100") -> OrderBook:
    return OrderBook(
        condition_id=cid,
        yes_asks=(Level(Decimal(yes), Decimal(size)),),
        no_asks=(Level(Decimal(no), Decimal(size)),),
    )


class FakeMarketSource:
    def __init__(self, markets: list[Market]) -> None:
        self._markets = markets

    def discover_markets(self, selector: object) -> list[Market]:
        return list(self._markets)


class FakeBookSource:
    def __init__(self, books: dict[str, OrderBook]) -> None:
        self._books = books

    def get_order_book(self, condition_id: str, yes_token_id: str, no_token_id: str) -> OrderBook:
        return self._books[condition_id]


class FakeExecutor(Executor):
    """Completes whatever it's handed; records the sized opportunity."""

    def __init__(self) -> None:
        self.executed: list[Opportunity] = []

    def execute(self, opportunity: Opportunity) -> ExecutionResult:
        self.executed.append(opportunity)
        return ExecutionResult(
            opportunity=opportunity,
            fills=(),
            completed=True,
            realized_edge=opportunity.net_edge_per_share,
            note="paper-fill",
        )

    @property
    def open_exposure(self) -> Decimal:
        return Decimal(0)


def _runner(markets, books, factory, *, observation_log=None, **risk_kwargs):
    cfg = Config(risk=RiskConfig(**risk_kwargs)) if risk_kwargs else Config()
    return ArbRunner(
        cfg,
        FakeMarketSource(markets),
        FakeBookSource(books),
        factory,
        observation_log=observation_log,
        clock=lambda: __import__("datetime").date(2026, 5, 25),
        sleep=lambda _s: None,
    )


def test_scan_executes_a_real_arb():
    m = _market("c1")
    book = _arb_book("c1", "0.40", "0.55")  # sum 0.95 -> 0.05 gross edge
    ex = FakeExecutor()
    runner = _runner([m], {"c1": book}, lambda _m, _b: ex)

    results = runner.scan_once()

    assert len(results) == 1
    assert results[0].completed
    assert len(ex.executed) == 1
    assert runner.total_exposure > 0


def test_no_arb_when_sum_exceeds_one():
    m = _market("c1")
    book = _arb_book("c1", "0.60", "0.55")  # sum 1.15 -> no edge
    ex = FakeExecutor()
    runner = _runner([m], {"c1": book}, lambda _m, _b: ex)

    assert runner.scan_once() == []
    assert ex.executed == []
    assert runner.total_exposure == 0


def test_inactive_market_skipped():
    m = _market("c1")
    inactive = Market("c2", "Q?", "Politics", "c2-yes", "c2-no", active=False)
    book = _arb_book("c1", "0.40", "0.55")
    ex = FakeExecutor()
    runner = _runner(
        [inactive, m],
        {"c1": book, "c2": _arb_book("c2", "0.40", "0.55")},
        lambda _m, _b: ex,
    )

    runner.scan_once()
    # only the active market executed
    assert {o.condition_id for o in ex.executed} == {"c1"}


def test_total_exposure_cap_halts_further_markets():
    m1, m2 = _market("c1"), _market("c2")
    books = {
        "c1": _arb_book("c1", "0.40", "0.55"),
        "c2": _arb_book("c2", "0.40", "0.55"),
    }
    ex = FakeExecutor()
    # Tiny cap: first market's pair eats the whole budget, second is skipped.
    runner = _runner(
        [m1, m2],
        books,
        lambda _m, _b: ex,
        max_total_exposure=Decimal("1"),
        max_trade_notional=Decimal("5"),
    )

    runner.scan_once()
    assert {o.condition_id for o in ex.executed} == {"c1"}
    assert runner.total_exposure <= Decimal("1")


def test_book_fetch_error_does_not_kill_loop():
    class ExplodingBooks:
        def get_order_book(self, *a):
            raise RuntimeError("network down")

    m = _market("c1")
    ex = FakeExecutor()
    cfg = Config()
    runner = ArbRunner(
        cfg, FakeMarketSource([m]), ExplodingBooks(), lambda _m, _b: ex, sleep=lambda _s: None
    )
    assert runner.scan_once() == []  # swallowed, no raise


def test_logs_near_miss_even_when_not_actionable(tmp_path):
    import json

    from polymarket_bot.common.observation_log import ObservationLog

    m = _market("c1")
    # sum 1.005 -> not actionable, but a near miss that must be logged.
    book = _arb_book("c1", "0.50", "0.505")
    log = ObservationLog(tmp_path / "s.jsonl", near_miss_gap=Decimal("0.01"))
    ex = FakeExecutor()
    runner = _runner([m], {"c1": book}, lambda _m, _b: ex, observation_log=log)

    results = runner.scan_once()
    assert results == []  # nothing executed
    assert ex.executed == []
    rec = json.loads((tmp_path / "s.jsonl").read_text().splitlines()[0])
    assert rec["condition_id"] == "c1"
    assert rec["ask_sum"] == "1.005"
    assert rec["actionable"] is False and rec["executed"] is False


def test_logs_executed_arb(tmp_path):
    import json

    from polymarket_bot.common.observation_log import ObservationLog

    m = _market("c1")
    book = _arb_book("c1", "0.40", "0.55")  # actionable arb
    log = ObservationLog(tmp_path / "s.jsonl")
    ex = FakeExecutor()
    runner = _runner([m], {"c1": book}, lambda _m, _b: ex, observation_log=log)

    runner.scan_once()
    rec = json.loads((tmp_path / "s.jsonl").read_text().splitlines()[0])
    assert rec["actionable"] is True and rec["executed"] is True
    assert rec["realized_edge"] is not None


def test_run_bounded_by_max_scans():
    m = _market("c1")
    book = _arb_book("c1", "0.40", "0.55")
    calls = {"n": 0}

    def factory(_m, _b):
        calls["n"] += 1
        return FakeExecutor()

    # Big budget so the exposure cap doesn't halt execution before 3 scans.
    runner = _runner([m], {"c1": book}, factory, max_total_exposure=Decimal("1000"))
    runner.run(max_scans=3)
    assert calls["n"] == 3
