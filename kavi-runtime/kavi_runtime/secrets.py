"""macOS Keychain secret store wrapper (item #C of 2026-05-06 hygiene pass).

Why: today the MS Graph token cache and Anthropic API key live as plain JSON /
env-var files on Kavi's Mac. A full disk theft + macOS unlock could exfiltrate
both. macOS Keychain is system-level encrypted and ties access to the user
account; moving the secrets there is a near-free hardening step.

Design:
- Primary backend: macOS Keychain via `keyring` package.
- Fallback backend: filesystem JSON under `~/.config/kavi/secrets.json`.
  Used in CI, in tests, and on platforms where Keychain isn't available.
- Backend choice is automatic: try keyring first, fall back to filesystem
  on ImportError or backend errors. Tests can force the fallback by passing
  `backend="filesystem"`.

API:
- `read_secret(key, *, backend=None) -> str | None`
- `write_secret(key, value, *, backend=None) -> None`
- `delete_secret(key, *, backend=None) -> None`

Service identifier (Keychain "service") is `kavi-runtime`. Each secret is
keyed by `key` within that service.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from kavi_runtime.state_io import atomic_write_json

logger = logging.getLogger(__name__)

SERVICE = "kavi-runtime"

FALLBACK_PATH = Path.home() / ".config" / "kavi" / "secrets.json"


Backend = Literal["keyring", "filesystem"]


def _try_keyring():
    """Return the `keyring` module if available, else None. Cached on first
    call (good enough — we never swap backends mid-process)."""
    global _keyring_mod
    try:
        return _keyring_mod  # type: ignore[name-defined]
    except NameError:
        pass
    try:
        import keyring  # type: ignore
        _keyring_mod = keyring
    except ImportError:
        logger.info("keyring not installed; using filesystem fallback for secrets")
        _keyring_mod = None
    return _keyring_mod


def _resolve_backend(backend: Backend | None) -> Backend:
    if backend is not None:
        return backend
    return "keyring" if _try_keyring() is not None else "filesystem"


def _read_filesystem(key: str, path: Path = FALLBACK_PATH) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("secrets fallback file unreadable: %s", path)
        return None
    return data.get(key)


def _write_filesystem(key: str, value: str, path: Path = FALLBACK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data[key] = value
    atomic_write_json(path, data)
    # Tighten perms on the fallback file (owner read/write only).
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _delete_filesystem(key: str, path: Path = FALLBACK_PATH) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if key in data:
        data.pop(key)
        atomic_write_json(path, data)


def read_secret(key: str, *, backend: Backend | None = None,
                fallback_path: Path | None = None) -> str | None:
    """Return the secret stored under `key`, or None if not present.

    Two read-fallback paths matter on Kavi's Mac:
    1. Keychain raises (e.g., the GUI session isn't unlocked over SSH —
       error -25308) → fall through to filesystem.
    2. Keychain returns None (we never wrote the value because the prior
       write also raised) → fall through to filesystem so a value that
       lives only in the filesystem fallback is still readable.

    Without case 2, a host that fails Keychain writes silently fails reads
    too, even when the filesystem fallback has the value.
    """
    chosen = _resolve_backend(backend)
    if chosen == "keyring":
        kr = _try_keyring()
        if kr is None:
            logger.warning("keyring backend requested but unavailable; falling back to filesystem")
            return _read_filesystem(key, fallback_path or FALLBACK_PATH)
        try:
            value = kr.get_password(SERVICE, key)
        except Exception as e:  # pragma: no cover — hard to trigger in tests
            logger.warning("keyring read failed for %s: %s; falling back to filesystem", key, e)
            return _read_filesystem(key, fallback_path or FALLBACK_PATH)
        if value is not None:
            return value
        # Keychain returned None — fall through to filesystem fallback.
        return _read_filesystem(key, fallback_path or FALLBACK_PATH)
    return _read_filesystem(key, fallback_path or FALLBACK_PATH)


def write_secret(key: str, value: str, *, backend: Backend | None = None,
                 fallback_path: Path | None = None) -> None:
    """Store `value` under `key`."""
    chosen = _resolve_backend(backend)
    if chosen == "keyring":
        kr = _try_keyring()
        if kr is None:
            _write_filesystem(key, value, fallback_path or FALLBACK_PATH)
            return
        try:
            kr.set_password(SERVICE, key, value)
            return
        except Exception as e:  # pragma: no cover
            logger.warning("keyring write failed for %s: %s; falling back to filesystem", key, e)
            _write_filesystem(key, value, fallback_path or FALLBACK_PATH)
            return
    _write_filesystem(key, value, fallback_path or FALLBACK_PATH)


def delete_secret(key: str, *, backend: Backend | None = None,
                  fallback_path: Path | None = None) -> None:
    chosen = _resolve_backend(backend)
    if chosen == "keyring":
        kr = _try_keyring()
        if kr is None:
            _delete_filesystem(key, fallback_path or FALLBACK_PATH)
            return
        try:
            kr.delete_password(SERVICE, key)
            return
        except Exception as e:
            # delete_password raises if the key doesn't exist; that's fine — treat
            # as success.
            logger.debug("keyring delete: %s (treating as no-op)", e)
            return
    _delete_filesystem(key, fallback_path or FALLBACK_PATH)
