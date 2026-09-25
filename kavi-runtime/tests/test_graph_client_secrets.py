"""Tests for graph_client secrets-module wiring (item #1 of 2026-05-06 loose-ends pass).

graph_client._load_cache reads MSAL token caches via kavi_runtime.secrets,
falling back to per-account files and then a legacy single-account file.
graph_client._save_cache dual-writes to Keychain (best-effort) AND a per-
account file (durable backup).

These tests force the filesystem backend on the secrets module so they do
not touch the developer's macOS Keychain.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kavi_runtime import graph_client as gc
from kavi_runtime import secrets as kavi_secrets


# Build a minimal config compatible with GraphClient construction.
def _config(tmp_path: Path) -> dict:
    return {
        "paths": {
            "household_md": str(tmp_path / "household.md"),
        },
        "graph": {
            "subscription_lifetime_minutes": 4230,
            "subscription_renewal_buffer_minutes": 60,
        },
    }


@pytest.fixture
def isolated_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect graph_client + secrets to a tmp_path-rooted layout so a
    real ~/.config/kavi/* never participates in the test."""
    token_dir = tmp_path / "tokens"
    sub_dir = tmp_path / "subscriptions"
    fallback_secrets = tmp_path / "secrets.json"
    monkeypatch.setattr(gc, "TOKEN_CACHE_DIR", token_dir)
    monkeypatch.setattr(gc, "SUBSCRIPTION_STATE_DIR", sub_dir)
    monkeypatch.setattr(gc, "LEGACY_TOKEN_CACHE_PATH", tmp_path / "legacy_token.json")
    monkeypatch.setattr(gc, "LEGACY_SUBSCRIPTION_STATE_PATH", tmp_path / "legacy_sub.json")
    monkeypatch.setattr(kavi_secrets, "FALLBACK_PATH", fallback_secrets)
    # Force filesystem backend so we don't touch real Keychain.
    monkeypatch.setattr(kavi_secrets, "_try_keyring", lambda: None)
    # Write a stub household.md so discover_household_accounts doesn't crash.
    (tmp_path / "household.md").write_text(
        "## Email identities\n"
        "| Person | Addresses |\n"
        "| --- | --- |\n"
        "| Megha | `megha@example.com` |\n"
    )
    return tmp_path


def _serialized_msal_cache_blob() -> str:
    """A minimal serialized MSAL token-cache shape — empty top-level dicts
    are fine for round-trip-equality testing of `cache.serialize()` /
    `cache.deserialize()`. We avoid populating account/token entries
    because that would require valid signature blobs."""
    from msal import SerializableTokenCache
    cache = SerializableTokenCache()
    # Mark it dirty so serialize emits non-trivial output.
    cache.deserialize('{"AccessToken": {}, "Account": {}, "IdToken": {}, "RefreshToken": {}, "AppMetadata": {}}')
    return cache.serialize()


def test_load_cache_reads_via_secrets_module_when_keychain_has_value(isolated_tmp: Path) -> None:
    """When secrets.read_secret returns a serialized cache, graph_client
    should use it without consulting the per-account file."""
    account = "megha@example.com"
    blob = _serialized_msal_cache_blob()
    # Pre-seed the secrets fallback (the test forces filesystem backend
    # so this is the "Keychain" surrogate).
    kavi_secrets.write_secret(
        f"graph_token_cache:{account}", blob,
        backend="filesystem", fallback_path=isolated_tmp / "secrets.json",
    )

    client = gc.GraphClient(_config(isolated_tmp), accounts=[account])
    # The cache for this account should reflect the Keychain blob.
    cache = client._caches[account]
    # Round-trip equality: serialized form should match what we wrote.
    assert cache.serialize() == blob


def test_load_cache_falls_back_to_per_account_file_on_keychain_miss(
    isolated_tmp: Path,
) -> None:
    """When Keychain has nothing for the account, graph_client should
    fall back to the per-account JSON file under TOKEN_CACHE_DIR."""
    account = "megha@example.com"
    blob = _serialized_msal_cache_blob()
    # Write only to per-account file; nothing to Keychain.
    file_path = gc.token_cache_path_for(account)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(blob)

    client = gc.GraphClient(_config(isolated_tmp), accounts=[account])
    cache = client._caches[account]
    assert cache.serialize() == blob


def test_secrets_module_falls_back_to_filesystem_when_keychain_unavailable(
    tmp_path: Path,
) -> None:
    """The secrets module's filesystem fallback returns the same value
    written via the keychain-backend code path when keyring is missing."""
    fp = tmp_path / "secrets.json"
    # With keyring forced unavailable, write_secret should still round-trip
    # through the filesystem backend.
    with patch.object(kavi_secrets, "_try_keyring", lambda: None):
        kavi_secrets.write_secret("graph_token_cache:foo@bar.com", "BLOB-X",
                                  fallback_path=fp)
        got = kavi_secrets.read_secret("graph_token_cache:foo@bar.com",
                                       fallback_path=fp)
        assert got == "BLOB-X"
