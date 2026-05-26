"""Tests for common.auth — credential derivation/caching, all mocked (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from polymarket_bot.common import auth
from polymarket_bot.common.auth import (
    AuthError,
    ClobCreds,
    cache_creds,
    derive_creds,
    get_creds,
    load_cached_creds,
)

FAKE_CREDS = ClobCreds(api_key="k", api_secret="s", api_passphrase="p")


def test_clobcreds_roundtrip():
    d = FAKE_CREDS.to_dict()
    assert ClobCreds.from_dict(d) == FAKE_CREDS
    assert set(d) == {"api_key", "api_secret", "api_passphrase"}


def test_cache_and_load(tmp_path):
    path = tmp_path / ".clob_creds.json"
    cache_creds(FAKE_CREDS, path)
    assert load_cached_creds(path) == FAKE_CREDS


def test_load_missing_returns_none(tmp_path):
    assert load_cached_creds(tmp_path / "nope.json") is None


def test_load_corrupt_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert load_cached_creds(path) is None


def test_derive_creds_uses_clob_client_no_network(monkeypatch):
    raw = SimpleNamespace(api_key="ak", api_secret="as", api_passphrase="ap")
    captured = {}

    class FakeClient:
        def create_or_derive_api_creds(self):
            return raw

    def fake_build(private_key):
        captured["key"] = private_key
        return FakeClient()

    monkeypatch.setattr(auth, "_build_l1_client", fake_build)
    creds = derive_creds("0xPRIVATE")
    assert creds == ClobCreds("ak", "as", "ap")
    assert captured["key"] == "0xPRIVATE"


def test_derive_creds_none_raises(monkeypatch):
    class FakeClient:
        def create_or_derive_api_creds(self):
            return None

    monkeypatch.setattr(auth, "_build_l1_client", lambda k: FakeClient())
    with pytest.raises(AuthError):
        derive_creds("0xkey")


def test_get_creds_uses_cache_first(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    cache_creds(FAKE_CREDS, path)

    def boom(_):
        raise AssertionError("should not derive when cache is present")

    monkeypatch.setattr(auth, "derive_creds", boom)
    assert get_creds(path=path) == FAKE_CREDS


def test_get_creds_derives_and_caches_when_missing(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    monkeypatch.setattr(auth, "wallet_private_key", lambda: "0xkey")
    monkeypatch.setattr(auth, "derive_creds", lambda pk: FAKE_CREDS)

    out = get_creds(path=path)
    assert out == FAKE_CREDS
    assert json.loads(path.read_text())["api_key"] == "k"


def test_get_creds_refresh_bypasses_cache(tmp_path, monkeypatch):
    path = tmp_path / "creds.json"
    cache_creds(FAKE_CREDS, path)
    new = ClobCreds("k2", "s2", "p2")
    monkeypatch.setattr(auth, "wallet_private_key", lambda: "0xkey")
    monkeypatch.setattr(auth, "derive_creds", lambda pk: new)

    assert get_creds(path=path, refresh=True) == new
    assert load_cached_creds(path) == new


def test_get_creds_missing_key_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "wallet_private_key", lambda: None)
    with pytest.raises(AuthError):
        get_creds(path=tmp_path / "creds.json")
