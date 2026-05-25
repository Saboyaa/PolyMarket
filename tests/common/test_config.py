from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from polymarket_bot.common.config import Config


def test_defaults_are_educational_scale():
    cfg = Config()
    assert cfg.mode == "paper"
    assert cfg.is_live is False
    assert cfg.risk.max_total_exposure == Decimal("10")
    assert cfg.risk.max_trade_notional == Decimal("5")
    assert cfg.risk.min_net_edge_per_share == Decimal("0.005")
    assert cfg.market_selector.selects_all is True


def test_load_from_toml(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(
        'mode = "live"\n'
        "scan_interval = 2\n"
        "[risk]\n"
        "max_total_exposure = 25\n"
        "max_trade_notional = 8\n"
        "[market_selector]\n"
        'categories = ["Politics"]\n'
        "[fees.rate_override]\n"
        "Crypto = 0.07\n"
    )
    cfg = Config.load(p)
    assert cfg.is_live is True
    assert cfg.risk.max_total_exposure == Decimal("25")
    # money parsed without float drift
    assert cfg.fees.rate_override["Crypto"] == Decimal("0.07")
    assert cfg.market_selector.selects_all is False
    assert cfg.market_selector.categories == ["Politics"]


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.toml")


def test_no_path_uses_defaults():
    assert Config.load().mode == "paper"


def test_negative_cap_rejected(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text("[risk]\nmax_total_exposure = -1\n")
    with pytest.raises(ValidationError):
        Config.load(p)
