import json
from datetime import UTC, datetime
from decimal import Decimal

from polymarket_bot.common.observation_log import Observation, ObservationLog


def _obs(gross: str, *, actionable=False, executed=False, realized=None) -> Observation:
    # yes/no asks are illustrative; gross_edge drives the near-miss filter.
    return Observation(
        condition_id="c1",
        category="Politics",
        yes_ask=Decimal("0.50"),
        no_ask=Decimal("0.50"),
        size=Decimal("100"),
        gross_edge_per_share=Decimal(gross),
        net_edge_per_share=Decimal(gross) - Decimal("0.001"),
        actionable=actionable,
        executed=executed,
        realized_edge=None if realized is None else Decimal(realized),
    )


def _fixed_clock():
    return datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def test_records_arb_and_near_miss(tmp_path):
    log = ObservationLog(tmp_path / "s.jsonl", near_miss_gap=Decimal("0.01"), clock=_fixed_clock)
    assert log.record(_obs("0.02")) is True  # gross arb (sum 0.98)
    assert log.record(_obs("-0.005")) is True  # near miss (sum 1.005, within gap)
    assert log.record(_obs("0")) is True  # exactly parity (sum 1.00)

    lines = (tmp_path / "s.jsonl").read_text().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["ask_sum"] == "1.00"
    assert first["gross_edge_per_share"] == "0.02"
    assert first["ts"] == "2026-05-26T12:00:00+00:00"


def test_drops_far_from_parity(tmp_path):
    path = tmp_path / "s.jsonl"
    log = ObservationLog(path, near_miss_gap=Decimal("0.01"))
    assert log.record(_obs("-0.05")) is False  # sum 1.05 -> too far, dropped
    assert not path.exists()  # nothing written at all


def test_rolling_cap_trims_oldest(tmp_path):
    path = tmp_path / "s.jsonl"
    log = ObservationLog(path, max_records=10, near_miss_gap=Decimal("0.01"))
    # Write well past the cap; trimming happens in batches.
    for _ in range(50):
        log.record(_obs("0.02"))
    log._trim()  # force a final trim
    lines = path.read_text().splitlines()
    assert len(lines) == 10


def test_money_fields_are_strings_for_precision(tmp_path):
    path = tmp_path / "s.jsonl"
    log = ObservationLog(path)
    log.record(_obs("0.005", actionable=True, executed=True, realized="0.004"))
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["net_edge_per_share"] == "0.004"
    assert rec["realized_edge"] == "0.004"
    assert rec["actionable"] is True and rec["executed"] is True
