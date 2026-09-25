"""Tests for kavi_runtime.secrets — Keychain wrapper with filesystem fallback.

We force the filesystem backend in tests to avoid side effects on the
developer's actual macOS Keychain. The keyring backend is exercised by
the integration test on Kavi's Mac via the migration script."""

from __future__ import annotations

from pathlib import Path

from kavi_runtime.secrets import (
    delete_secret,
    read_secret,
    write_secret,
)


def test_filesystem_backend_round_trip(tmp_path: Path) -> None:
    """Write then read returns the same value."""
    fp = tmp_path / "secrets.json"
    write_secret("graph_token_cache:megha@example.com", "TOKEN-A",
                 backend="filesystem", fallback_path=fp)
    got = read_secret("graph_token_cache:megha@example.com",
                      backend="filesystem", fallback_path=fp)
    assert got == "TOKEN-A"


def test_filesystem_backend_returns_none_for_missing(tmp_path: Path) -> None:
    fp = tmp_path / "secrets.json"
    got = read_secret("nonexistent", backend="filesystem", fallback_path=fp)
    assert got is None


def test_filesystem_backend_supports_multiple_keys(tmp_path: Path) -> None:
    """Several keys coexist in one fallback file."""
    fp = tmp_path / "secrets.json"
    write_secret("graph_token_cache:megha@example.com", "MEGHA-TOKEN",
                 backend="filesystem", fallback_path=fp)
    write_secret("graph_token_cache:max@example.com", "MAX-TOKEN",
                 backend="filesystem", fallback_path=fp)
    write_secret("anthropic_api_key", "sk-ant-...",
                 backend="filesystem", fallback_path=fp)
    assert read_secret("graph_token_cache:megha@example.com",
                       backend="filesystem", fallback_path=fp) == "MEGHA-TOKEN"
    assert read_secret("graph_token_cache:max@example.com",
                       backend="filesystem", fallback_path=fp) == "MAX-TOKEN"
    assert read_secret("anthropic_api_key",
                       backend="filesystem", fallback_path=fp) == "sk-ant-..."


def test_filesystem_backend_delete(tmp_path: Path) -> None:
    """Delete removes the key but leaves siblings intact."""
    fp = tmp_path / "secrets.json"
    write_secret("k1", "v1", backend="filesystem", fallback_path=fp)
    write_secret("k2", "v2", backend="filesystem", fallback_path=fp)
    delete_secret("k1", backend="filesystem", fallback_path=fp)
    assert read_secret("k1", backend="filesystem", fallback_path=fp) is None
    assert read_secret("k2", backend="filesystem", fallback_path=fp) == "v2"
