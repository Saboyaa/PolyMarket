import json
from datetime import UTC, datetime
from decimal import Decimal

from polymarket_bot.common.models import InventoryState, Level, Market, OrderBook, Quote
from polymarket_bot.phase2_market_making.quote_log import QuoteLog

_TS = datetime(2026, 5, 26, tzinfo=UTC)


def _market():
    return Market("c1", "Q?", "Politics", "y", "n")


def _book():
    return OrderBook(
        condition_id="c1",
        yes_asks=(Level(Decimal("0.52"), Decimal("100")),),
        yes_bids=(Level(Decimal("0.48"), Decimal("100")),),
    )


def _quote():
    return Quote("c1", Decimal("0.49"), Decimal("0.51"), Decimal("10"), Decimal("10"))


def test_records_quote_row_money_as_strings(tmp_path):
    log = QuoteLog(tmp_path / "mm.jsonl", clock=lambda: _TS)
    inv = InventoryState("c1", net_yes=Decimal("5"), rebates_earned=Decimal("0.02"))
    log.record(_market(), _book(), _quote(), inv)

    rec = json.loads((tmp_path / "mm.jsonl").read_text().splitlines()[0])
    assert rec["condition_id"] == "c1" and rec["quoted"] is True
    assert rec["mid"] == "0.50" and rec["bid"] == "0.49" and rec["half_spread"] == "0.01"
    assert rec["net_yes"] == "5" and rec["rebates_earned"] == "0.02"
    assert isinstance(rec["net_pnl"], str)  # money serialized as string


def test_records_stopped_row_with_null_quote(tmp_path):
    log = QuoteLog(tmp_path / "mm.jsonl", clock=lambda: _TS)
    log.record(_market(), _book(), None, InventoryState("c1"))
    rec = json.loads((tmp_path / "mm.jsonl").read_text().splitlines()[0])
    assert rec["quoted"] is False
    assert rec["bid"] is None and rec["ask"] is None and rec["half_spread"] is None
    assert rec["mid"] == "0.50"  # still records the observed mid


def test_rolling_cap_trims_oldest(tmp_path):
    log = QuoteLog(tmp_path / "mm.jsonl", max_records=200, clock=lambda: _TS)
    for _ in range(600):
        log.record(_market(), _book(), _quote(), InventoryState("c1"))
    lines = (tmp_path / "mm.jsonl").read_text().splitlines()
    # trims in batches, so it never exceeds the cap by more than one batch (~25%)
    assert 200 <= len(lines) <= 250
