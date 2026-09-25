"""One-shot setup: MS Graph OAuth (device-flow) + create webhook subscription.

Run once per Mac (or whenever the token cache is wiped or the subscription expires
unrecoverably). Subsequent runs of kavi-runtime auto-renew the subscription before
expiration.

    uv run python scripts/setup_graph_auth.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from msal import PublicClientApplication, SerializableTokenCache

load_dotenv(Path.home() / ".config" / "kavi" / ".env")

CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools (public)
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Read", "Mail.Send", "Tasks.ReadWrite"]  # offline_access added implicitly by msal

TOKEN_CACHE_PATH = Path.home() / ".config" / "kavi" / "graph_token_cache.json"
SUBSCRIPTION_STATE_PATH = Path.home() / ".config" / "kavi" / "graph_subscription.json"


def load_or_init_cache() -> SerializableTokenCache:
    cache = SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    return cache


def save_cache(cache: SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())
        TOKEN_CACHE_PATH.chmod(0o600)


def authenticate() -> tuple[str, SerializableTokenCache]:
    cache = load_or_init_cache()
    app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            print(f"silent_auth_ok: account={accounts[0]['username']}")
            save_cache(cache)
            return result["access_token"], cache

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow init failed: {flow}")

    print("\n" + "=" * 60)
    print(flow["message"])
    print("=" * 60 + "\n")
    print("Waiting for authentication... (this script blocks until you finish)\n")

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"device flow auth failed: {result}")

    save_cache(cache)
    print(f"auth_ok: account={result.get('id_token_claims', {}).get('preferred_username', '?')}")
    return result["access_token"], cache


def create_subscription(access_token: str) -> dict:
    # Funnel URL lives in the private config.yaml (server.public_url).
    import yaml
    with open(Path(__file__).resolve().parent.parent / "config.yaml") as f:
        public_url = yaml.safe_load(f)["server"]["public_url"]
    notification_url = f"{public_url}/graph/notifications"
    client_state = secrets.token_urlsafe(32)
    expiration = (datetime.now(timezone.utc) + timedelta(minutes=4230)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )

    payload = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": "/me/mailFolders/inbox/messages",
        "expirationDateTime": expiration,
        "clientState": client_state,
    }

    resp = httpx.post(
        "https://graph.microsoft.com/v1.0/subscriptions",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )

    if resp.status_code not in (200, 201):
        print(f"subscription_create_failed: status={resp.status_code}")
        print(f"body: {resp.text[:500]}")
        sys.exit(1)

    sub = resp.json()
    state = {
        "subscription_id": sub["id"],
        "client_state": client_state,
        "expiration_dt": sub["expirationDateTime"],
        "notification_url": notification_url,
        "resource": sub["resource"],
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    SUBSCRIPTION_STATE_PATH.write_text(json.dumps(state, indent=2))
    SUBSCRIPTION_STATE_PATH.chmod(0o600)
    print(f"subscription_created: id={sub['id'][:12]}... expires={sub['expirationDateTime']}")
    return state


def main() -> int:
    print(f"setup_graph_auth: starting at {datetime.now().isoformat()}")
    access_token, _cache = authenticate()
    if SUBSCRIPTION_STATE_PATH.exists():
        existing = json.loads(SUBSCRIPTION_STATE_PATH.read_text())
        print(f"existing_subscription_found: id={existing['subscription_id'][:12]}...")
        print("delete it on Graph side first if you want a fresh one.")
    state = create_subscription(access_token)
    print(f"\nready: notification_url={state['notification_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
