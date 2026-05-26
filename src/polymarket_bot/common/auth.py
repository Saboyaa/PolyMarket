"""Derive and cache CLOB API credentials from the wallet private key.

Polymarket's CLOB uses two-tier auth: an L1 signature (from the wallet private
key) is used once to derive/create a set of L2 API credentials (key / secret /
passphrase). Those L2 creds are what every authenticated request actually uses.

This module derives the L2 creds via ``py-clob-client`` and caches them to a
git-ignored JSON file so we do not have to re-derive (and re-sign) every run.

Security: the wallet private key is read only from the environment via
``common.config.wallet_private_key`` and is **never** written to disk or logged.
Only the derived L2 creds are cached.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

from polymarket_bot.common.config import wallet_private_key

logger = logging.getLogger(__name__)

# Public CLOB host and chain for Polymarket (Polygon mainnet).
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = POLYGON

# Git-ignored cache location (see .gitignore: .clob_creds.json).
DEFAULT_CREDS_PATH = Path(".clob_creds.json")


class AuthError(RuntimeError):
    """Raised when credentials cannot be derived (e.g. missing wallet key)."""


@dataclass(frozen=True)
class ClobCreds:
    """Derived L2 CLOB API credentials. Never includes the private key."""

    api_key: str
    api_secret: str
    api_passphrase: str

    def to_dict(self) -> dict[str, str]:
        return {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "api_passphrase": self.api_passphrase,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> ClobCreds:
        return cls(
            api_key=data["api_key"],
            api_secret=data["api_secret"],
            api_passphrase=data["api_passphrase"],
        )


def _build_l1_client(private_key: str) -> ClobClient:
    """Construct an L1 (signing) client from the wallet key.

    Isolated for easy mocking in tests so no network/signing happens by default.
    """
    return ClobClient(CLOB_HOST, chain_id=CHAIN_ID, key=private_key)


def load_cached_creds(path: Path = DEFAULT_CREDS_PATH) -> ClobCreds | None:
    """Return cached creds if the cache file exists and is valid, else None."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return ClobCreds.from_dict(data)
    except (json.JSONDecodeError, KeyError, OSError):
        logger.warning("Ignoring unreadable CLOB creds cache at %s", path)
        return None


def cache_creds(creds: ClobCreds, path: Path = DEFAULT_CREDS_PATH) -> None:
    """Write derived creds to the git-ignored cache with tight permissions."""
    path.write_text(json.dumps(creds.to_dict(), indent=2))
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        pass


def derive_creds(private_key: str) -> ClobCreds:
    """Derive (or create) L2 CLOB creds from the wallet key via the clob client.

    Performs network I/O through ``py-clob-client``; mocked in default tests.
    """
    client = _build_l1_client(private_key)
    raw = client.create_or_derive_api_creds()
    if raw is None:
        raise AuthError("CLOB client returned no credentials")
    return ClobCreds(
        api_key=raw.api_key,
        api_secret=raw.api_secret,
        api_passphrase=raw.api_passphrase,
    )


def get_creds(
    *,
    path: Path = DEFAULT_CREDS_PATH,
    refresh: bool = False,
) -> ClobCreds:
    """Return CLOB creds, using the cache unless ``refresh`` is set.

    Reads the wallet key from the environment only when a derivation is needed.
    The private key is never logged nor persisted.
    """
    if not refresh:
        cached = load_cached_creds(path)
        if cached is not None:
            logger.debug("Using cached CLOB creds from %s", path)
            return cached

    private_key = wallet_private_key()
    if not private_key:
        raise AuthError("POLYMARKET_WALLET_KEY is not set; cannot derive CLOB credentials.")

    creds = derive_creds(private_key)
    cache_creds(creds, path)
    logger.info("Derived and cached CLOB credentials (private key never stored).")
    return creds
