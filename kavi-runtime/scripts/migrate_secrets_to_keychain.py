"""One-time migration: copy MS Graph token caches + Anthropic API key into
macOS Keychain via kavi_runtime.secrets, verify read-back, then archive
the source files (rename with .migrated-<date>.bak suffix; do NOT delete).

Usage on Kavi's Mac:
    cd /Users/kavi/kavi-runtime
    .venv/bin/python -m scripts.migrate_secrets_to_keychain

The script is idempotent: re-running after a successful migration finds
nothing to migrate and exits cleanly.

Per item #C constraints: archive (rename), do NOT delete. Megha can delete
the .bak files after verifying the runtime still authenticates.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from kavi_runtime.secrets import read_secret, write_secret

KAVI_CONFIG_DIR = Path.home() / ".config" / "kavi"
TOKEN_CACHE_DIR = KAVI_CONFIG_DIR / "tokens"
LEGACY_TOKEN_CACHE = KAVI_CONFIG_DIR / "graph_token_cache.json"

ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"


def migrate_graph_tokens() -> dict[str, str]:
    """For every Graph token cache JSON file we find on disk, write its
    contents into Keychain under key `graph_token_cache:<account>`.
    Returns a map of {migrated_filename: keychain_key}.

    Files migrated:
    - Per-account caches at ~/.config/kavi/tokens/<email>.json
    - Legacy single-account cache at ~/.config/kavi/graph_token_cache.json
    """
    migrated: dict[str, str] = {}

    # Per-account caches
    if TOKEN_CACHE_DIR.exists():
        for path in TOKEN_CACHE_DIR.glob("*.json"):
            account = path.stem  # filename without .json
            key = f"graph_token_cache:{account}"
            payload = path.read_text()
            write_secret(key, payload)
            # Verify read-back
            roundtrip = read_secret(key)
            if roundtrip != payload:
                print(f"[FAIL] read-back mismatch for {key}; LEAVING source file in place")
                continue
            migrated[str(path)] = key
            print(f"[OK] migrated {path} -> Keychain key {key}")

    # Legacy single-account cache
    if LEGACY_TOKEN_CACHE.exists():
        key = "graph_token_cache:legacy"
        payload = LEGACY_TOKEN_CACHE.read_text()
        write_secret(key, payload)
        roundtrip = read_secret(key)
        if roundtrip != payload:
            print(f"[FAIL] read-back mismatch for {key}; LEAVING source file in place")
        else:
            migrated[str(LEGACY_TOKEN_CACHE)] = key
            print(f"[OK] migrated {LEGACY_TOKEN_CACHE} -> Keychain key {key}")

    return migrated


def migrate_anthropic_key() -> bool:
    """If the ANTHROPIC_API_KEY env var is set and the Keychain doesn't
    already have it, store it. Returns True on a write."""
    env_value = os.environ.get(ANTHROPIC_KEY_ENV)
    if not env_value:
        print("[skip] ANTHROPIC_API_KEY not set in env; nothing to migrate")
        return False
    existing = read_secret("anthropic_api_key")
    if existing == env_value:
        print("[skip] anthropic_api_key already in Keychain and matches env")
        return False
    write_secret("anthropic_api_key", env_value)
    if read_secret("anthropic_api_key") != env_value:
        print("[FAIL] anthropic_api_key read-back mismatch")
        return False
    print("[OK] anthropic_api_key written to Keychain")
    return True


def archive_migrated_files(paths: list[str]) -> None:
    """Rename each migrated source file with a .migrated-<UTC-date>.bak suffix.
    Do NOT delete — the user can clean up after verifying the runtime still
    authenticates."""
    if not paths:
        return
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for src in paths:
        src_path = Path(src)
        dest = src_path.with_suffix(src_path.suffix + f".migrated-{date}.bak")
        if dest.exists():
            print(f"[skip archive] {dest} already exists; not clobbering")
            continue
        shutil.move(str(src_path), str(dest))
        print(f"[archived] {src_path} -> {dest}")


def main() -> int:
    print("== Kavi runtime: migrate secrets to macOS Keychain ==")
    migrated = migrate_graph_tokens()
    migrate_anthropic_key()
    if migrated:
        archive_migrated_files(list(migrated.keys()))
    else:
        print("[done] no Graph token caches found to migrate")
    print("Done. Verify runtime authentication after restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
