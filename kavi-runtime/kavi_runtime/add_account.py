"""CLI entrypoint for onboarding a new Microsoft account into the Kavi runtime.

Usage:
    .venv/bin/python -m kavi_runtime.add_account <account_email> [--seed-handle <handle>]

What it does, in order:
    1. Runs the MSAL device-code flow against the consumers Microsoft tenant
       so the human at the keyboard can sign in to that mailbox. The terminal
       prints the verification URL plus a 9-character code; the user enters
       the code at that URL on any device, signs in, approves consent.
    2. Persists the resulting token to a per-account cache at
       ~/.config/kavi/tokens/<account>.json (file mode 600).
    3. Creates a Microsoft Graph webhook subscription on
       /me/mailFolders/inbox/messages for that account, pointed at the
       runtime's existing public Tailscale Funnel notification endpoint.
       Persists subscription state to ~/.config/kavi/subscriptions/<account>.json.
    4. (Optional, --seed-handle) Seeds the new member's Messages.app chat
       on Kavi so BlueBubbles registers the chat in its local chat.db.
       Without this seed, BlueBubbles silent-drops outbound iMessages for
       any handle that has never been mirrored. When run on Kavi's Mac the
       seed is attempted via `osascript` against Messages.app; otherwise
       the operator gets clear printed instructions to run the seed
       manually on Kavi's Mac.

This script is interactive by design — Megha and Max sit at the keyboard
together for ~2 minutes per account. Subsequent runtime restarts pick up
the cached token and refresh silently.

After both household accounts have been onboarded, restart the runtime so
the scheduler picks up the new subscription (the scheduler iterates the
configured accounts at startup and registers a renewal job per account):

    launchctl kickstart -k gui/501/com.megha.kavi
"""

from __future__ import annotations

import json
import logging
import platform
import secrets
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from msal import PublicClientApplication, SerializableTokenCache

from kavi_runtime.graph_client import (
    AUTHORITY,
    CLIENT_ID,
    SCOPES,
    subscription_state_path_for,
    token_cache_path_for,
)
from kavi_runtime import household
from kavi_runtime.state_io import atomic_write_json, atomic_write_text
from kavi_runtime.state_schemas import SubscriptionState

logger = logging.getLogger(__name__)

# Resolved at runtime from the live config so the public URL matches whatever
# the rest of the runtime is using (avoids a second source-of-truth drift).
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
DEFAULT_ENV_PATH = Path.home() / ".config" / "kavi" / ".env"


def _print_banner(message: str) -> None:
    print("\n" + "=" * 60)
    print(message)
    print("=" * 60 + "\n")


def authenticate_account(account_email: str) -> tuple[str, SerializableTokenCache]:
    """Run MSAL device-code flow for one account. Persists the cache on
    success. Returns (access_token, cache) so the caller can reuse the
    token to create the webhook subscription without an extra round trip."""
    cache_path = token_cache_path_for(account_email)
    cache = SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())

    app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            already = accounts[0].get("username", "?")
            print(f"silent_auth_ok: account already in cache as {already}")
            if already.lower() != account_email.lower():
                print(
                    f"WARNING: cached identity {already!r} does not match "
                    f"requested {account_email!r}. Falling back to interactive flow."
                )
            else:
                _persist_cache(cache, cache_path)
                return result["access_token"], cache

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"device flow init failed: {flow}")

    _print_banner(flow["message"])
    print(
        f"Hand the keyboard (or phone) to {account_email}. The script blocks "
        f"until consent is granted; press Ctrl+C to abort."
    )

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"device flow auth failed: {result}")

    signed_in = result.get("id_token_claims", {}).get("preferred_username", "?")
    if signed_in.lower() != account_email.lower():
        raise RuntimeError(
            f"signed_in_account_mismatch: expected {account_email}, got {signed_in}. "
            f"Token was NOT cached. Re-run and sign in as the right user."
        )

    _persist_cache(cache, cache_path)
    print(f"auth_ok: account={signed_in}")
    return result["access_token"], cache


def _persist_cache(cache: SerializableTokenCache, path: Path) -> None:
    atomic_write_text(path, cache.serialize())
    path.chmod(0o600)


def create_subscription(
    access_token: str, account_email: str, public_url: str, lifetime_minutes: int,
) -> dict:
    """Create the MS Graph webhook subscription on /me/mailFolders/inbox/messages
    for this account. Persists the subscription state under the per-account
    path so the scheduler can renew it."""
    notification_url = f"{public_url}/graph/notifications"
    client_state = secrets.token_urlsafe(32)
    expiration = (datetime.now(timezone.utc) + timedelta(minutes=lifetime_minutes)).strftime(
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
        "account": account_email,
        "subscription_id": sub["id"],
        "client_state": client_state,
        "expiration_dt": sub["expirationDateTime"],
        "notification_url": notification_url,
        "resource": sub["resource"],
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = subscription_state_path_for(account_email)
    atomic_write_json(path, state, schema=SubscriptionState)
    path.chmod(0o600)
    print(
        f"subscription_created: account={account_email} id={sub['id'][:12]}... "
        f"expires={sub['expirationDateTime']} state_path={path}"
    )
    return state


def _is_running_on_kavis_mac() -> bool:
    """Best-effort check that this script is running on Kavi's MacBook (and
    therefore that an `osascript` Messages.app seed will actually fire from
    Kavi's Apple ID, not from a different Mac signed in to a different
    iCloud). The runtime's canonical hostname is `kavis-macbook-pro`."""
    if platform.system() != "Darwin":
        return False
    try:
        host = socket.gethostname().lower()
    except Exception:
        return False
    return "kavis-macbook-pro" in host or host.startswith("kavi")


def seed_imessage_chat(handle: str) -> dict[str, str]:
    """Seed a new household member's chat in BlueBubbles' chat.db by sending
    one iMessage from Messages.app on Kavi's Mac. Without this seed,
    `bb.send_with_verify` returns `verified=false, message_guid=null,
    send_response.status=timeout` for that handle — BlueBubbles silent-drops
    sends to unmirrored chats.

    Two implementation paths considered:

      (a) AppleScript-drive Messages.app on Kavi (this function). Sends from
          Messages.app directly, which writes chat.db. Only works when run
          on Kavi's Mac with Automation grant for Terminal -> Messages.
      (b) BlueBubbles `POST /api/v1/chat/new`. BlueBubbles v1 has no
          documented `chat/new` endpoint that creates the chat in chat.db
          from a cold start — only `message/text` (which silent-drops on
          unseeded chats). Path (b) was ruled out as unreliable.

    Returns a small status dict: {"approach": "osascript"|"manual",
    "ok": "true"|"false", "detail": "<message>"}. Never raises — failure
    falls through to printed instructions and the caller decides whether
    to proceed.
    """
    if not _is_running_on_kavis_mac():
        msg = (
            "Not running on Kavi's Mac (hostname does not match "
            "`kavis-macbook-pro`). Cannot drive Messages.app from here. "
            "Run the manual seed step (see banner below)."
        )
        logger.info("seed_imessage_chat: %s", msg)
        return {"approach": "manual", "ok": "false", "detail": msg}

    script = (
        'tell application "Messages" to send "Welcome from Kavi" '
        f'to buddy "{handle}" of service "iMessage"'
    )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        msg = (
            f"osascript timed out seeding chat for {handle}. Likely "
            "Automation grant missing for Terminal -> Messages, or "
            "Messages.app is not signed in to Kavi's Apple ID. Run the "
            "manual seed step (see banner below)."
        )
        logger.warning("seed_imessage_chat: %s", msg)
        return {"approach": "osascript", "ok": "false", "detail": msg}
    except Exception as e:
        msg = f"osascript invocation failed for {handle}: {e!r}"
        logger.warning("seed_imessage_chat: %s", msg)
        return {"approach": "osascript", "ok": "false", "detail": msg}

    if result.returncode != 0:
        msg = (
            f"osascript exit={result.returncode} seeding chat for {handle}. "
            f"stderr={result.stderr.strip()[:200]}. Likely Automation grant "
            "missing for Terminal -> Messages, or Messages.app is not "
            "signed in to Kavi's Apple ID."
        )
        logger.warning("seed_imessage_chat: %s", msg)
        return {"approach": "osascript", "ok": "false", "detail": msg}

    msg = (
        f"Sent 'Welcome from Kavi' to {handle} from Messages.app on Kavi. "
        "Chat is now mirrored in chat.db and `send_with_verify` should "
        "return verified=true with a message_guid."
    )
    logger.info("seed_imessage_chat: %s", msg)
    return {"approach": "osascript", "ok": "true", "detail": msg}


def _print_manual_seed_instructions(handle: str) -> None:
    _print_banner(
        f"Manual chat-seed required for {handle}\n\n"
        "On Kavi's MacBook ('kavis-macbook-pro'), open Messages.app and "
        f"send one iMessage to {handle} from Kavi's Apple ID "
        f"({household.kavi_apple_id()}). Body content does not matter — any "
        "single iMessage seeds the chat in BlueBubbles' chat.db. After the "
        "seed lands, every future Kavi-to-recipient iMessage will route "
        "and verify correctly."
    )


def _parse_argv(argv: list[str]) -> tuple[str | None, str | None]:
    """Lightweight arg parsing — keeps this module zero-dependency past
    yaml + httpx + msal. Returns (account_email, seed_handle); either
    may be None."""
    account_email: str | None = None
    seed_handle: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--seed-handle" and i + 1 < len(argv):
            seed_handle = argv[i + 1].strip()
            i += 2
            continue
        if a.startswith("--seed-handle="):
            seed_handle = a.split("=", 1)[1].strip()
            i += 1
            continue
        if account_email is None and not a.startswith("--"):
            account_email = a.strip().lower()
        i += 1
    return account_email, seed_handle


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    account_email, seed_handle = _parse_argv(argv)
    if not account_email or "@" not in account_email:
        print(
            f"add_account: bad account {account_email!r}; expected an email address "
            "(optionally followed by --seed-handle <handle>)"
        )
        return 2

    load_dotenv(DEFAULT_ENV_PATH)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config_path = DEFAULT_CONFIG_PATH
    if not config_path.exists():
        print(f"add_account: config not found at {config_path}; aborting")
        return 1
    with open(config_path) as f:
        config = yaml.safe_load(f)

    public_url = config.get("server", {}).get("public_url")
    if not public_url:
        print("add_account: config.server.public_url missing; cannot create subscription")
        return 1
    lifetime_minutes = config.get("graph", {}).get("subscription_lifetime_minutes", 4230)

    print(f"add_account: starting at {datetime.now().isoformat()} for {account_email}")

    access_token, _cache = authenticate_account(account_email)

    state_path = subscription_state_path_for(account_email)
    if state_path.exists():
        existing = json.loads(state_path.read_text())
        print(
            f"existing_subscription_found: account={account_email} id={existing['subscription_id'][:12]}... "
            f"expires={existing.get('expiration_dt')}"
        )
        print(
            "Existing subscription kept. Delete the JSON file at the path above "
            "if you want a fresh one created on the next run."
        )
        return 0
    state = create_subscription(access_token, account_email, public_url, lifetime_minutes)

    print(
        f"\nAccount {account_email} added. Token cached at "
        f"{token_cache_path_for(account_email)}. Subscription created on "
        f"/me/mailFolders/inbox/messages with id {state['subscription_id']}."
    )

    # Step 4: chat-seed for the new member's iMessage handle.
    # Without this, BlueBubbles silent-drops Kavi -> recipient iMessages
    # for any handle that has never been mirrored in chat.db. The bug was
    # discovered 2026-06-02 during Max's intro-message send.
    if seed_handle:
        print(f"\nStep 4: seeding iMessage chat for {seed_handle} ...")
        result = seed_imessage_chat(seed_handle)
        print(f"  approach={result['approach']} ok={result['ok']}")
        print(f"  {result['detail']}")
        if result["ok"] != "true":
            _print_manual_seed_instructions(seed_handle)
    else:
        print(
            "\n(no --seed-handle provided; skipping chat-seed step. If this "
            "is a new household member who will receive Kavi-sent iMessages, "
            "open Messages.app on Kavi's Mac and send one manual iMessage "
            "to their handle from Kavi's Apple ID before relying on "
            "Kavi-to-recipient delivery.)"
        )

    print(
        "\nNext step: restart the runtime so the scheduler picks up this account.\n"
        "  launchctl kickstart -k gui/501/com.megha.kavi"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
