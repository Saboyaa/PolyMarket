"""Typed configuration for the bot, loaded from TOML + environment.

Secrets (wallet private key) come from the environment / ``.env`` and never from
the TOML file. All risk caps live here and are freely changeable; the defaults are
deliberately tiny (educational scale).
"""

from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RiskConfig(BaseModel):
    """Risk caps, all in USDC. Enforced by ``common.risk`` before any order."""

    max_total_exposure: Decimal = Field(default=Decimal("10"), gt=0)
    max_trade_notional: Decimal = Field(default=Decimal("5"), gt=0)
    min_net_edge_per_share: Decimal = Field(default=Decimal("0.005"), ge=0)
    max_completion_slippage: Decimal = Field(default=Decimal("0.02"), ge=0)

    @field_validator(
        "max_total_exposure",
        "max_trade_notional",
        "min_net_edge_per_share",
        "max_completion_slippage",
        mode="before",
    )
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        # Accept ints/floats/strings from TOML without float rounding drift.
        return v if isinstance(v, Decimal) else Decimal(str(v))


class MarketSelector(BaseModel):
    """Which markets to scan. Empty selector = scan all active markets."""

    condition_ids: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    @property
    def selects_all(self) -> bool:
        return not self.condition_ids and not self.categories


class FeesConfig(BaseModel):
    """Optional override of the per-category taker feeRate table.

    Empty ``rate_override`` => use the built-in effective-dated schedule.
    """

    rate_override: dict[str, Decimal] = Field(default_factory=dict)

    @field_validator("rate_override", mode="before")
    @classmethod
    def _to_decimal_map(cls, v: object) -> dict[str, Decimal]:
        if not isinstance(v, dict):
            raise TypeError("fees.rate_override must be a table of category = rate")
        return {str(k): Decimal(str(val)) for k, val in v.items()}


class Config(BaseModel):
    """Top-level config. Load via :meth:`load`."""

    mode: Literal["paper", "live"] = "paper"
    scan_interval: float = Field(default=5.0, gt=0)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    market_selector: MarketSelector = Field(default_factory=MarketSelector)
    fees: FeesConfig = Field(default_factory=FeesConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Load config from a TOML file (if present), else use defaults.

        Environment variables are reserved for secrets and are read by
        ``common.auth``, not here, to keep this object secret-free.
        """
        data: dict = {}
        if path is not None:
            p = Path(path)
            if not p.exists():
                raise FileNotFoundError(f"Config file not found: {p}")
            with p.open("rb") as fh:
                data = tomllib.load(fh)
        return cls.model_validate(data)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def wallet_private_key() -> str | None:
    """Read the wallet private key from the environment. Never logged."""
    return os.environ.get("POLYMARKET_WALLET_KEY")
