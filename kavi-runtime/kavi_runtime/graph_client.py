"""Microsoft Graph wrapper. Mail read, To Do CRUD, subscription management.

Multi-account support (added 2026-05-05 evening, Step 3 of the build order).
Each Microsoft account (Megha, Max, ...) carries its own MSAL token cache,
its own MS Graph subscription, and its own per-account view of the shared
McMullen-Jain Shared list (Microsoft assigns a different list ID per
account because the list is shared, not owned). The runtime routes every
incoming webhook + every outbound Graph call through this object with an
explicit `account` parameter so a notification firing on Max's mailbox
acts on Max's token + Max's view of the list and never crosses streams.

Auth via msal device-flow. The first time an account is added, run
`python -m kavi_runtime.add_account <email>` interactively. Subsequent
runtime starts pick up the persisted token cache and refresh silently.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from msal import PublicClientApplication, SerializableTokenCache

from kavi_runtime import secrets as kavi_secrets
from kavi_runtime.state_io import atomic_write_json, atomic_write_text
from kavi_runtime.state_schemas import SubscriptionState

logger = logging.getLogger(__name__)


def _record_tool_call(
    tool: str, args: dict[str, Any], result: dict[str, Any], duration_ms: int,
) -> None:
    """Forward a tool-call into the active unified Trace (Phase B,
    2026-05-12) AND stamp runtime_status' last-graph-call tracker so the
    `/status` page can show when MS Graph last responded successfully.
    No-op when no Trace is active. Failure-safe: trace_log or status
    exceptions must never break the graph call path."""
    try:
        from kavi_runtime.trace_log import current as _trace_current
        _t = _trace_current()
        if _t is not None:
            _t.record_tool_call(tool, args, result, duration_ms=duration_ms)
    except Exception as e:
        logger.debug("graph_client: trace tool-call mirror failed: %s", e)
    try:
        from kavi_runtime.runtime_status import record_graph_call_ok
        record_graph_call_ok(tool)
    except Exception as e:
        logger.debug("graph_client: runtime_status mirror failed: %s", e)


def _record_graph_read_ok(tool: str) -> None:
    """Stamp runtime_status' last-graph-call tracker on a successful read
    (Graph responded, status 2xx or 4xx — both prove reachability).
    Reads do not flow through the Trace, so this helper is the read-side
    analog of _record_tool_call's status hook. Failure-safe."""
    try:
        from kavi_runtime.runtime_status import record_graph_call_ok
        record_graph_call_ok(tool)
    except Exception as e:
        logger.debug("graph_client: runtime_status mirror failed: %s", e)


def _token_cache_secret_key(account_email: str) -> str:
    """Stable Keychain key for an account's MSAL token cache. Matches the
    key the migration script (scripts/migrate_secrets_to_keychain.py)
    writes to: `graph_token_cache:<account-email>` (using the same
    `_safe_filename` shape so per-account files map 1:1 with Keychain
    entries)."""
    return f"graph_token_cache:{_safe_filename(account_email)}"

CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Read", "Mail.Send", "Tasks.ReadWrite"]

# Per-account token caches and per-account subscription state files. The
# legacy paths below kept the single-account names so an existing Megha
# install keeps working without a token-cache migration on first run.
TOKEN_CACHE_DIR = Path.home() / ".config" / "kavi" / "tokens"
SUBSCRIPTION_STATE_DIR = Path.home() / ".config" / "kavi" / "subscriptions"
LEGACY_TOKEN_CACHE_PATH = Path.home() / ".config" / "kavi" / "graph_token_cache.json"
LEGACY_SUBSCRIPTION_STATE_PATH = Path.home() / ".config" / "kavi" / "graph_subscription.json"

GRAPH = "https://graph.microsoft.com/v1.0"

# Sentinel message-id prefix used by the post-restart E2E smoke probe
# (capabilities/realtime_kavi/runtime_health.py builds its synthetic
# notification ids from this constant). fetch_message treats any 4xx on
# an id with this prefix as a clean skip — Graph answers 400 Bad Request
# (malformed id), not 404, for these.
SMOKE_TEST_MESSAGE_ID_PREFIX = "SMOKE_TEST_"


class OutboundContentBlocked(Exception):
    """Raised when the outbound content scanner blocks a To Do task write.

    Surfaces from `GraphClient.create_todo_task` and
    `GraphClient.create_task_in_shared_list` so the calling handler (email
    arrival, iMessage create-task verb, manual missed correction,
    coordination 4b) hits its existing exception path. The block is
    independently audit-logged to outbound_blocked.jsonl by the gate; the
    exception just stops the call site from continuing as if the write
    succeeded.
    """

# The shared list's display name. Each account discovers its own list ID by
# matching this name against the result of GET /me/todo/lists. Cached on the
# GraphClient instance to avoid the round trip on every call.
SHARED_LIST_NAME = "McMullen-Jain Shared"

# Owner abbreviations stamped on the front of Kavi-rendered task titles. Kept here
# (duplicated from handlers.OWNER_ABBREV) so graph_client stays import-clean.
_KAVI_OWNER_ABBREVS = {"MJ", "MM"}


def _safe_filename(account_email: str) -> str:
    """Render an email address into a filesystem-safe filename. The '@' is
    preserved (legible at a glance) but stripped of slashes / nulls / shell
    metacharacters so a malformed account string can never escape its
    intended directory."""
    cleaned = "".join(
        c for c in account_email.strip().lower() if c.isalnum() or c in "@.-_+"
    )
    return cleaned or "unknown"


def token_cache_path_for(account_email: str) -> Path:
    """Return the on-disk token cache path for one account. Per-account so a
    refresh on Megha's token never touches Max's. The single-account legacy
    cache is migrated lazily on first GraphClient access (see _load_cache)."""
    return TOKEN_CACHE_DIR / f"{_safe_filename(account_email)}.json"


def subscription_state_path_for(account_email: str) -> Path:
    """Return the on-disk subscription state path for one account."""
    return SUBSCRIPTION_STATE_DIR / f"{_safe_filename(account_email)}.json"


def _populate_target_from_legacy(default_account: str) -> bool:
    """Phase 1 of the legacy subscription migration: write the per-account
    file from the legacy single-account file if the target doesn't exist.

    Returns True if the target now exists (whether by this call or already).
    Returns False if neither legacy nor target exist (no migration possible).

    Idempotent: safe to call on every boot.
    """
    target = subscription_state_path_for(default_account)
    if target.exists():
        return True
    if not LEGACY_SUBSCRIPTION_STATE_PATH.exists():
        return False
    try:
        legacy = json.loads(LEGACY_SUBSCRIPTION_STATE_PATH.read_text())
    except json.JSONDecodeError as exc:
        logger.error(
            "graph_client: legacy subscription state at %s is corrupt; skipping "
            "forward-migration. Run add_account.py to re-create. %s",
            LEGACY_SUBSCRIPTION_STATE_PATH, exc,
        )
        return False
    required = {"subscription_id", "expiration_dt", "client_state"}
    missing = required - set(legacy.keys())
    if missing:
        logger.error(
            "graph_client: legacy subscription state at %s missing required keys %s; "
            "skipping forward-migration. Run add_account.py to re-create.",
            LEGACY_SUBSCRIPTION_STATE_PATH, missing,
        )
        return False
    legacy.setdefault("account", default_account)
    atomic_write_json(target, legacy, schema=SubscriptionState)
    target.chmod(0o600)
    return True


def _archive_legacy_if_orphaned() -> None:
    """Phase 2 of the legacy subscription migration: if the legacy file is
    still present, rename it to .migrated-<ts>.bak so no code path reads it.

    Runs unconditionally every boot. The first boot after migration archives
    the legacy file. If a previous boot crashed BETWEEN target write and
    legacy archive (P2.8 transactional gap), subsequent boots clean it up.

    Idempotent: no-op if legacy file is absent.
    """
    if not LEGACY_SUBSCRIPTION_STATE_PATH.exists():
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = LEGACY_SUBSCRIPTION_STATE_PATH.with_name(
        LEGACY_SUBSCRIPTION_STATE_PATH.name + f".migrated-{ts}.bak"
    )
    try:
        LEGACY_SUBSCRIPTION_STATE_PATH.rename(archived)
        logger.info(
            "graph_client: archived legacy subscription state %s -> %s",
            LEGACY_SUBSCRIPTION_STATE_PATH, archived,
        )
    except OSError as exc:
        logger.warning(
            "graph_client: legacy archive rename failed (%s); will retry on next boot. "
            "Benign — load_subscription_state no longer reads legacy.",
            exc,
        )


def _migrate_legacy_subscription_state_forward(default_account: str) -> None:
    """Forward-migrate legacy graph_subscription.json into subscriptions/
    <default-account>.json, then archive the legacy file. Two idempotent
    phases that can recover from a crash between them (P2.8 transactional
    migration).

    Why: 2026-05-06 the runtime came up with subscriptions/ empty and the
    legacy file orphaned. The fix removes the fallback path so subscription
    state lives in exactly one place — but requires forward-migrating the
    legacy file once at startup. Splitting into separate populate + archive
    phases means a crash between them is recoverable on the next boot.
    """
    target_ready = _populate_target_from_legacy(default_account)
    if target_ready:
        _archive_legacy_if_orphaned()


def _strip_kavi_title_prefixes(title: str) -> str:
    """Strip the `[?] ` low-conf marker, the owner abbreviation (`MJ ` / `MM `),
    and a leading `[Tag] ` source-tag from a Kavi-rendered task title, returning
    the human-readable body. Used by find_task_by_exact_title so Megha can say
    "Oak Circle volunteering" and match the stored "MJ Oak Circle volunteering".

    Mirrors handlers._strip_kavi_prefix (kept duplicated to avoid a circular
    import; both are 8-line helpers and the title format is stable).
    """
    rest = title
    if rest.startswith("[?] "):
        rest = rest[4:]
    if len(rest) >= 3 and rest[2] == " " and rest[:2] in _KAVI_OWNER_ABBREVS:
        rest = rest[3:]
    if rest.startswith("["):
        end = rest.find("] ")
        if end != -1:
            rest = rest[end + 2:]
    return rest


def discover_household_accounts(config: dict) -> list[str]:
    """Read household.md and return the list of Microsoft account email
    addresses Kavi should authenticate against. Convention: the first
    address in the "Email identities" table for each household member is
    the primary scanned account; aliases are NOT separate Graph accounts
    (Outlook merges them into one mailbox).

    Falls back to the config's pair (Megha + Max primaries) when the file
    cannot be parsed — better to keep running than to lose the household
    on a transient parse error.
    """
    household_path = Path(config.get("paths", {}).get("household_md", "")) if config else Path()
    from kavi_runtime import household as _household
    fallback = [_household.primary_email("megha"), _household.primary_email("max")]
    if not household_path or not household_path.exists():
        logger.info(
            "discover_household_accounts: household.md not found at %s; using fallback %s",
            household_path, fallback,
        )
        return fallback
    try:
        text = household_path.read_text()
    except Exception as e:
        logger.warning(
            "discover_household_accounts: read failed (%s); using fallback %s", e, fallback,
        )
        return fallback

    # Walk the "Email identities" table. The convention in household.md:
    # "| Person | `addr@x` (primary, scanned), `alias@x` ..."
    # We pick the first backticked address per data row that appears below
    # a header that starts with "Email identities".
    in_email_section = False
    accounts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##") and "Email identities" in stripped:
            in_email_section = True
            continue
        if in_email_section and stripped.startswith("##"):
            break
        if not in_email_section or not stripped.startswith("|"):
            continue
        # Header / divider rows: skip
        if "Person" in stripped and "Address" in stripped:
            continue
        if set(stripped.replace("|", "").strip()) <= {"-", " "}:
            continue
        # Pick the first backticked email on the row.
        first_tick = stripped.find("`")
        if first_tick == -1:
            continue
        second_tick = stripped.find("`", first_tick + 1)
        if second_tick == -1:
            continue
        candidate = stripped[first_tick + 1 : second_tick].strip().lower()
        if "@" in candidate:
            accounts.append(candidate)

    if not accounts:
        logger.info("discover_household_accounts: parse found no addresses; using fallback")
        return fallback
    logger.info("discover_household_accounts: discovered %s", accounts)
    return accounts


class GraphClient:
    """Multi-account Microsoft Graph client.

    Construction: pass the runtime config plus an optional list of account
    emails. When `accounts` is None, the client discovers them from
    household.md via `discover_household_accounts`. Each account has its
    own MSAL token cache and its own (lazily resolved) shared-list ID.

    Backward-compatible API: every method accepts an `account` keyword
    argument; when omitted, the call routes through the default account
    (the first one discovered, typically Megha). Existing call sites that
    have not yet been threaded with an account argument keep working
    against Megha's stream.
    """

    def __init__(self, config: dict, accounts: list[str] | None = None):
        self._config = config
        self._sub_lifetime = config["graph"]["subscription_lifetime_minutes"]
        self._sub_renewal_buffer = config["graph"]["subscription_renewal_buffer_minutes"]
        self._accounts: list[str] = list(accounts) if accounts else discover_household_accounts(config)
        if not self._accounts:
            raise RuntimeError("GraphClient: no household accounts discovered")
        self._default_account = self._accounts[0]

        # Per-account state holders. Token caches are loaded lazily so an
        # account that has never been authed (no cache file yet) does not
        # crash construction; it crashes only when its first method call
        # tries to acquire a token.
        self._caches: dict[str, SerializableTokenCache] = {}
        self._apps: dict[str, PublicClientApplication] = {}
        self._shared_list_id_cache: dict[str, str] = {}
        self._inbox_folder_id_cache: dict[str, str | None] = {}

        TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SUBSCRIPTION_STATE_DIR.mkdir(parents=True, exist_ok=True)

        # Forward-migrate single-account legacy subscription state into the
        # per-account location so no code path reads the legacy file again.
        _migrate_legacy_subscription_state_forward(self._default_account)

        for acct in self._accounts:
            self._caches[acct] = self._load_cache(acct)
            self._apps[acct] = PublicClientApplication(
                CLIENT_ID, authority=AUTHORITY, token_cache=self._caches[acct],
            )

        # Pre-seed the shared-list ID for the default account from config
        # so existing single-account call sites that pass `list_id` from
        # config continue to work without a probe.
        legacy_list_id = config.get("graph", {}).get("mstodo_shared_list_id")
        if legacy_list_id:
            self._shared_list_id_cache[self._default_account] = legacy_list_id

    # ---- account helpers ----------------------------------------------

    def accounts(self) -> list[str]:
        """All configured Microsoft accounts, in discovery order."""
        return list(self._accounts)

    @property
    def default_account(self) -> str:
        return self._default_account

    def _resolve_account(self, account: str | None) -> str:
        if account is None:
            return self._default_account
        a = account.strip().lower()
        if a not in self._accounts:
            raise ValueError(
                f"GraphClient: unknown account {account!r}; configured: {self._accounts}"
            )
        return a

    def has_token(self, account: str) -> bool:
        """True when the account's MSAL cache has at least one signed-in
        identity. A False result means OAuth still needs to run for that
        account (the scheduler skips the renewal job for that account
        until then)."""
        a = self._resolve_account(account)
        return bool(self._apps[a].get_accounts())

    # ---- per-account token cache --------------------------------------

    def _load_cache(self, account: str) -> SerializableTokenCache:
        """Load this account's MSAL token cache.

        Lookup order (2026-05-06 secrets-module wiring):
          1. macOS Keychain via `secrets.read_secret(graph_token_cache:<acct>)`.
             Keychain is system-encrypted + user-account-scoped, so it's the
             primary backend on Kavi's Mac after migration.
          2. Per-account JSON file at `~/.config/kavi/tokens/<acct>.json`.
             Acts as a backup so an existing install keeps authenticating
             through migration; also covers fresh installs that ran
             `add_account` before Keychain was wired in.
          3. Legacy single-account JSON at `~/.config/kavi/graph_token_cache.json`,
             but only for the default account (legacy cache held one identity).

        On any successful read, returns a deserialized cache. If we read
        from a fallback path, we DO NOT auto-promote into Keychain — that's
        the migration script's job, run once per host.
        """
        cache = SerializableTokenCache()

        # 1. Keychain
        try:
            blob = kavi_secrets.read_secret(_token_cache_secret_key(account))
        except Exception as e:
            logger.warning("graph_client: secrets.read_secret failed for %s: %s", account, e)
            blob = None
        if blob:
            try:
                cache.deserialize(blob)
                return cache
            except Exception as e:
                logger.warning(
                    "graph_client: keychain blob for %s did not deserialize (%s); "
                    "falling back to filesystem", account, e,
                )

        # 2. Per-account file
        path = token_cache_path_for(account)
        if path.exists():
            try:
                cache.deserialize(path.read_text())
                return cache
            except Exception as e:
                logger.warning(
                    "graph_client: per-account cache for %s did not deserialize (%s); "
                    "falling back to legacy", account, e,
                )

        # 3. Legacy single-account fallback (default account only)
        if account == self._default_account and LEGACY_TOKEN_CACHE_PATH.exists():
            try:
                cache.deserialize(LEGACY_TOKEN_CACHE_PATH.read_text())
                logger.info(
                    "graph_client: read legacy token cache for default account %s", account,
                )
                # Persist into the new per-account location immediately so
                # subsequent saves write back to a stable location.
                atomic_write_text(path, cache.serialize())
                path.chmod(0o600)
            except Exception as e:
                logger.warning("graph_client: legacy token cache read failed: %s", e)
        return cache

    def _save_cache(self, account: str) -> None:
        """Persist this account's MSAL token cache.

        Dual-write: BOTH Keychain (primary, via `secrets.write_secret`) AND
        the per-account JSON file (backup). The dual path means a Keychain
        backend hiccup never loses tokens; conversely a filesystem theft
        without the macOS user unlock can't read the Keychain copy. The
        Keychain write is best-effort — we never let it block the file
        write because losing tokens would force a re-auth (annoying for
        Megha) whereas a missing Keychain entry is silently recoverable
        via the next `_load_cache` falling back to the file.
        """
        cache = self._caches[account]
        if not cache.has_state_changed:
            return
        serialized = cache.serialize()

        # File write (always) — this is the durable backup.
        path = token_cache_path_for(account)
        atomic_write_text(path, serialized)
        path.chmod(0o600)

        # Keychain write (best-effort).
        try:
            kavi_secrets.write_secret(_token_cache_secret_key(account), serialized)
        except Exception as e:
            logger.warning(
                "graph_client: secrets.write_secret failed for %s (file copy still saved): %s",
                account, e,
            )

    def _access_token(self, account: str) -> str:
        a = self._resolve_account(account)
        app = self._apps[a]
        accounts = app.get_accounts()
        if not accounts:
            raise RuntimeError(
                f"no graph identity in cache for {a}; "
                f"run python -m kavi_runtime.add_account {a}"
            )
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError(f"graph token refresh failed for {a}: {result}")
        self._save_cache(a)
        return result["access_token"]

    def _headers(self, advanced_query: bool = False, *, account: str | None = None) -> dict[str, str]:
        a = self._resolve_account(account)
        h = {"Authorization": f"Bearer {self._access_token(a)}"}
        if advanced_query:
            h["ConsistencyLevel"] = "eventual"
        return h

    # ---- per-account shared list resolution ---------------------------

    def resolve_shared_list_id(self, account: str | None = None) -> str:
        """Return the McMullen-Jain Shared list ID as seen from this
        account's MS To Do. Microsoft assigns a different list ID per
        account when a list is shared, so each account must look it up
        in its own /me/todo/lists. Cached after first resolution."""
        a = self._resolve_account(account)
        cached = self._shared_list_id_cache.get(a)
        if cached:
            return cached
        resp = httpx.get(
            f"{GRAPH}/me/todo/lists",
            headers=self._headers(account=a),
            params={"$top": 50},
            timeout=30.0,
        )
        resp.raise_for_status()
        wanted = SHARED_LIST_NAME.lower()
        for lst in resp.json().get("value", []):
            if (lst.get("displayName") or "").strip().lower() == wanted:
                self._shared_list_id_cache[a] = lst["id"]
                logger.info(
                    "graph_client: resolved shared list for account=%s id=%s",
                    a, lst["id"][:24],
                )
                return lst["id"]
        raise RuntimeError(
            f"graph_client: could not find list named {SHARED_LIST_NAME!r} for account {a}"
        )

    # ---- mail / thread fetch ------------------------------------------

    def fetch_message(self, message_id: str, *, account: str | None = None) -> dict[str, Any] | None:
        """Fetch one email from Graph. Returns None on 404 (message gone:
        deleted, moved out of /me, or already processed). Caller must
        treat None as "skip" — Microsoft replays buffered notifications
        during recovery from outages, and the referenced message often no
        longer exists in the inbox. Treating those as failures sprays
        false-alarm rate alerts (2026-05-06 PM incident).

        Smoke-test sentinel ids (`SMOKE_TEST_...`, see
        capabilities/realtime_kavi/runtime_health.py) also return None on
        ANY 4xx: Graph answers 400 Bad Request for them (malformed id, not
        not-found), and the design intent is the same clean skip as a 404.
        Before 2026-06-10 the 400 raised and every restart left one ERROR
        traceback in the err log, burying real ones.
        """
        # internetMessageHeaders added 2026-05-06 for inbox pre-filter REC-1
        # SHADOW mode (audits/token_optimization_2026-05-06.md). The pre-filter
        # checks `List-Unsubscribe` and `Auto-Submitted: auto-generated` headers
        # alongside the sender-domain denylist; both are RFC-standard
        # marketing/automation signals exposed by Graph as
        # `internetMessageHeaders: [{name, value}, ...]`. Token cost is small
        # (~10-30 headers per email, ~100-300 tokens) and the pre-filter saves
        # ~16K tokens per match by avoiding the Sonnet call entirely.
        select = "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,conversationId,parentFolderId,internetMessageHeaders"
        resp = httpx.get(
            f"{GRAPH}/me/messages/{message_id}",
            headers=self._headers(account=account),
            params={"$select": select},
            timeout=30.0,
        )
        _record_graph_read_ok("read:fetch_message")
        if resp.status_code == 404:
            logger.info(
                "graph_client.fetch_message: 404 for message_id=%s account=%s "
                "(stale notification — message no longer in inbox)",
                message_id[:16], account,
            )
            return None
        if (
            message_id.startswith(SMOKE_TEST_MESSAGE_ID_PREFIX)
            and 400 <= resp.status_code < 500
        ):
            logger.info(
                "graph_client.fetch_message: %d for smoke-test sentinel "
                "message_id=%s — expected, skipping cleanly",
                resp.status_code, message_id[:24],
            )
            return None
        resp.raise_for_status()
        return resp.json()

    def inbox_folder_id(self, *, account: str | None = None) -> str | None:
        """The Outlook inbox folder ID for one account. Cached per-account
        on first successful fetch.

        Returns None on lookup failure. Callers must treat None as "skip the
        folder check" rather than fail-closed: better to occasionally process
        a stray Junk email than to drop a real inbox event during a transient
        Graph hiccup. Used by email_arrived as a belt-and-suspenders filter
        against subscription-scope regression (added 2026-04-29)."""
        a = self._resolve_account(account)
        if a in self._inbox_folder_id_cache:
            return self._inbox_folder_id_cache[a]
        try:
            resp = httpx.get(
                f"{GRAPH}/me/mailFolders/inbox",
                headers=self._headers(account=a),
                params={"$select": "id"},
                timeout=10.0,
            )
            _record_graph_read_ok("read:inbox_folder_id")
            resp.raise_for_status()
            self._inbox_folder_id_cache[a] = resp.json().get("id")
        except Exception as e:
            logger.warning(
                "inbox_folder_id lookup failed for %s (will skip folder check): %s", a, e,
            )
            self._inbox_folder_id_cache[a] = None
        return self._inbox_folder_id_cache[a]

    def fetch_thread(self, conversation_id: str, *, account: str | None = None) -> list[dict[str, Any]]:
        select = "id,subject,from,toRecipients,receivedDateTime,bodyPreview"
        resp = httpx.get(
            f"{GRAPH}/me/messages",
            headers=self._headers(advanced_query=True, account=account),
            params={
                "$filter": f"conversationId eq '{conversation_id}'",
                "$select": select,
                "$top": 25,
                "$count": "true",
            },
            timeout=30.0,
        )
        _record_graph_read_ok("read:fetch_thread")
        resp.raise_for_status()
        return resp.json().get("value", [])

    def fetch_sentitems_replies(self, conversation_id: str, *, account: str | None = None) -> list[dict[str, Any]]:
        select = "id,toRecipients,sentDateTime,bodyPreview"
        resp = httpx.get(
            f"{GRAPH}/me/mailFolders/sentitems/messages",
            headers=self._headers(advanced_query=True, account=account),
            params={
                "$filter": f"conversationId eq '{conversation_id}'",
                "$select": select,
                "$top": 25,
                "$count": "true",
            },
            timeout=30.0,
        )
        _record_graph_read_ok("read:fetch_sentitems_replies")
        resp.raise_for_status()
        return resp.json().get("value", [])

    def list_messages_in_range(
        self,
        start_utc: str,
        end_utc: str,
        *,
        account: str | None = None,
        top_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """Page through /me/messages filtered by receivedDateTime in the
        given UTC window. Returns the full materialized list.

        Used by the one-shot date-range backfill script
        (scripts/backfill_email_range.py) to replay missed emails through
        the inbox-to-task classifier when a subscription gap leaves the
        webhook stream silent. Not on any hot path.

        `start_utc` and `end_utc` must be ISO-8601 strings ending in `Z`,
        e.g. `2026-05-14T00:00:00Z`. The filter is inclusive on both ends
        (`ge` / `le`); callers passing day boundaries should render end as
        `23:59:59Z` to include the last day's messages.
        """
        select = (
            "id,subject,from,toRecipients,receivedDateTime,bodyPreview,"
            "body,conversationId,parentFolderId,internetMessageHeaders"
        )
        # Manual $skip pagination. Re-send the full param set on every page
        # rather than following @odata.nextLink. Observed 2026-05-27: when
        # $select includes the message body, Graph's nextLink for page 2+
        # silently drops $filter, so following nextLink paginates the entire
        # inbox without the date constraint and the $skip cursor regresses to
        # a fixed value, creating an infinite loop. Manual $skip keeps every
        # request authoritative and bounded by the actual matching set.
        headers = self._headers(advanced_query=True, account=account)
        url = f"{GRAPH}/me/messages"
        out: list[dict[str, Any]] = []
        skip = 0
        while True:
            params: dict[str, Any] = {
                "$filter": f"receivedDateTime ge {start_utc} and receivedDateTime le {end_utc}",
                "$select": select,
                "$top": top_per_page,
                "$orderby": "receivedDateTime asc",
                "$skip": skip,
            }
            # Retry on JSON decode errors. Graph occasionally returns a
            # truncated stream when bodies push response size past a few
            # hundred KB.
            last_err: Exception | None = None
            body = None
            for attempt in range(3):
                resp = httpx.get(url, headers=headers, params=params, timeout=60.0)
                _record_graph_read_ok("read:list_messages_in_range")
                resp.raise_for_status()
                try:
                    body = resp.json()
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "list_messages_in_range: JSON decode failed on attempt %d/3: %s",
                        attempt + 1, e,
                    )
                    time.sleep(2 ** attempt)
            if body is None:
                raise RuntimeError(
                    f"list_messages_in_range: response failed to parse after 3 attempts; last error={last_err}"
                )
            page = body.get("value", [])
            out.extend(page)
            # Stop when the page is short (server returned fewer than
            # requested) or empty. Graph may cap the actual page size below
            # what $top requests; that is a partial-page signal, not the end.
            # Use the absence of @odata.nextLink in the body together with a
            # short page as the termination condition.
            next_link = body.get("@odata.nextLink")
            if not page or not next_link:
                break
            skip += len(page)
        return out

    # ---- subscription management (per-account) ------------------------

    def load_subscription_state(self, account: str | None = None) -> dict | None:
        """Load subscription state from the per-account file. Returns None
        when the file does not exist (account hasn't been onboarded) or is
        unreadable. The legacy single-account fallback was removed; any
        pre-existing legacy file is forward-migrated at GraphClient
        construction (see _migrate_legacy_subscription_state_forward)."""
        a = self._resolve_account(account)
        path = subscription_state_path_for(a)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            logger.error(
                "graph_client: subscription state at %s is corrupt: %s. "
                "Run add_account.py to re-create.", path, exc,
            )
            return None

    def renew_subscription_if_needed(self, account: str | None = None) -> None:
        """Renew the subscription for one account if it is approaching
        expiry. No-op when state is missing (the account hasn't been
        onboarded yet) so the scheduler can iterate every household
        account safely without crashing on a missing token."""
        a = self._resolve_account(account)
        state = self.load_subscription_state(a)
        if not state:
            logger.warning(
                "renew_subscription_if_needed: no subscription state for account=%s; "
                "run python -m kavi_runtime.add_account %s",
                a, a,
            )
            return
        if not self.has_token(a):
            logger.warning(
                "renew_subscription_if_needed: no token in cache for account=%s; skipping", a,
            )
            return
        expires = datetime.fromisoformat(state["expiration_dt"].replace("Z", "+00:00"))
        buffer = timedelta(minutes=self._sub_renewal_buffer)
        if datetime.now(timezone.utc) + buffer < expires:
            return
        new_expires = (datetime.now(timezone.utc) + timedelta(minutes=self._sub_lifetime)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        resp = httpx.patch(
            f"{GRAPH}/subscriptions/{state['subscription_id']}",
            headers={**self._headers(account=a), "Content-Type": "application/json"},
            json={"expirationDateTime": new_expires},
            timeout=30.0,
        )
        resp.raise_for_status()
        state["expiration_dt"] = new_expires
        path = subscription_state_path_for(a)
        atomic_write_json(path, state, schema=SubscriptionState)
        path.chmod(0o600)
        logger.info("subscription renewed for account=%s; new expiration %s", a, new_expires)

    def all_subscription_states(self) -> dict[str, dict | None]:
        """Return a snapshot of every account's subscription state. Used by
        the webhook endpoint to map an inbound notification's
        subscriptionId back to the originating account."""
        return {a: self.load_subscription_state(a) for a in self._accounts}

    def account_for_subscription(self, subscription_id: str | None) -> str | None:
        """Resolve a Graph subscription ID to its owning account email.
        Returns None when no configured account owns this subscription
        (the webhook router treats that as "drop with a warning")."""
        if not subscription_id:
            return None
        for a in self._accounts:
            state = self.load_subscription_state(a)
            if state and state.get("subscription_id") == subscription_id:
                return a
        return None

    # ---- to-do crud ---------------------------------------------------

    def find_todo_task_by_source_email(
        self, list_id: str, source_email_id: str, *, account: str | None = None,
    ) -> str | None:
        """Dedup check: scan recent tasks' linkedResources for matching externalId."""
        resp = httpx.get(
            f"{GRAPH}/me/todo/lists/{list_id}/tasks",
            headers=self._headers(account=account),
            params={"$top": 50, "$expand": "linkedResources", "$orderby": "lastModifiedDateTime desc"},
            timeout=30.0,
        )
        resp.raise_for_status()
        for t in resp.json().get("value", []):
            for lr in t.get("linkedResources", []) or []:
                if lr.get("externalId") == source_email_id:
                    return t["id"]
        return None

    def create_todo_task(
        self,
        list_id: str,
        task: dict[str, Any],
        source_email_id: str,
        source_subject: str,
        *,
        account: str | None = None,
    ) -> str:
        """Create a task with title rendered per task-writer-mstodo skill, then attach
        a linkedResource for dedup. Returns the new task id.

        Title format (updated 2026-04-29 evening): `<owner_abbrev> <title>` (no source-tag
        bracket — sender becomes obvious from the body once title says enough). Low-conf
        prepends `[?]`. The model is responsible for embedding deadlines in parens within
        the title body when the email implies one.

        Outbound-content scanner gate (added 2026-05-06): the rendered title +
        body is run through `gate_outbound_content` BEFORE the Graph POST. A
        sensitive-pattern hit (credit-card / SSN / routing / account number)
        blocks the write and raises `OutboundContentBlocked`. This is the
        single source of truth — every email path that lands a task funnels
        through here, so a regex-shaped numeric in an LLM-rendered title or
        body cannot reach MS To Do regardless of which caller invoked it.
        """
        _t0 = time.monotonic()
        owner_abbrev = {"megha": "MJ", "max": "MM"}.get(task["owner"], "??")
        title = f"{owner_abbrev} {task['title']}"
        if task.get("confidence") == "low":
            title = f"[?] {title}"

        body_content = f"Owner: {task['owner']} - {task.get('owner_reason', '')}\nFrom: {source_subject}"

        # Outbound-content gate. Lazy import to avoid a circular module dep
        # during runtime startup. Block runs BEFORE any Graph POST so a
        # sensitive-pattern hit never produces a half-created task. The gate
        # already audit-logs to outbound_blocked.jsonl on a hit; we add a
        # second row carrying the email_id extras so the audit trail can be
        # joined back to the source mail.
        from kavi_runtime.runtime import outbound_scanner
        gate_text = f"{title} {body_content}"
        allowed, reason = outbound_scanner.gate_outbound_content(
            config=self._config, text=gate_text, surface="todo_body",
        )
        if not allowed:
            outbound_scanner.log_blocked(
                config=self._config, surface="todo_body",
                reason=f"{reason}_email_context",
                text=gate_text,
                extras={"email_id": source_email_id, "account": account or self._default_account},
            )
            logger.warning(
                "graph_client.create_todo_task: BLOCKED by content scanner "
                "reason=%s email_id=%s", reason, (source_email_id or "")[:24],
            )
            raise OutboundContentBlocked(
                f"create_todo_task blocked: reason={reason} email_id={source_email_id}"
            )

        payload: dict[str, Any] = {
            "title": title,
            "body": {
                "contentType": "text",
                "content": body_content,
            },
        }
        if task.get("due"):
            payload["dueDateTime"] = {"dateTime": f"{task['due']}T00:00:00", "timeZone": "America/Los_Angeles"}

        resp = httpx.post(
            f"{GRAPH}/me/todo/lists/{list_id}/tasks",
            headers={**self._headers(account=account), "Content-Type": "application/json"},
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        task_id = resp.json()["id"]

        # Linked resource for dedup; non-fatal if it fails (task is real).
        try:
            httpx.post(
                f"{GRAPH}/me/todo/lists/{list_id}/tasks/{task_id}/linkedResources",
                headers={**self._headers(account=account), "Content-Type": "application/json"},
                json={
                    "externalId": source_email_id,
                    "applicationName": "HomeOS kavi-runtime",
                    "displayName": source_subject[:100],
                },
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            logger.warning("linkedResource attach failed (task created): %s", e)

        # Phase B trace mirror (2026-05-12).
        _record_tool_call(
            "graph.create_todo_task",
            {"list_id": list_id, "title": title, "owner": task.get("owner"),
             "source_email_id": source_email_id, "account": account},
            {"ok": True, "task_id": task_id},
            int((time.monotonic() - _t0) * 1000),
        )
        return task_id

    def create_task_in_shared_list(
        self,
        list_id: str,
        title: str,
        owner_prefix: str,
        deadline: datetime | None,
        source_imessage_id: str | None,
        *,
        account: str | None = None,
    ) -> tuple[str, bool]:
        """Action layer create-task verb (iMessage-to-task capability, 2026-05-05).
        Creates a new task in the McMullen-Jain Shared list rendered with the
        same title convention the email-to-task path uses (`<owner_prefix> <title>`),
        and attaches a linkedResource keyed on `source_imessage_id` for natural-key
        dedup.

        Returns (task_id, created) — `created=False` means a task already exists
        for this source_imessage_id and was returned from the dedup lookup; the
        caller can short-circuit the post-action reply accordingly.

        owner_prefix must be one of {"MJ", "MM"}; deadline is rendered to MS To Do
        timezone-aware due date when provided. The body field is plain "Source:
        iMessage from <owner>" — no email subject to thread through here.
        """
        if owner_prefix not in {"MJ", "MM"}:
            raise ValueError(f"create_task_in_shared_list: bad owner_prefix={owner_prefix!r}")
        if not title or not title.strip():
            raise ValueError("create_task_in_shared_list: empty title")

        # Natural-key dedup: a duplicate webhook fire on the same source_imessage_id
        # must NOT create a duplicate task. Same pattern as find_todo_task_by_source_email
        # for the email path.
        if source_imessage_id:
            existing = self.find_todo_task_by_source_email(
                list_id, source_imessage_id, account=account,
            )
            if existing:
                logger.info(
                    "create_task_in_shared_list: dedup hit on source_imessage_id=%s -> existing task %s",
                    source_imessage_id[:24], existing[:12],
                )
                return existing, False

        rendered_title = f"{owner_prefix} {title.strip()}"
        body_text = f"Source: iMessage from {'Megha' if owner_prefix == 'MJ' else 'Max'}"

        # Outbound-content gate (added 2026-05-06). Same scanner the iMessage
        # send wrapper uses — runs BEFORE the Graph POST. A sensitive-pattern
        # hit blocks the write so a card-shaped numeric Megha or Max typed
        # into iMessage cannot land in MS To Do.
        from kavi_runtime.runtime import outbound_scanner
        gate_text = f"{rendered_title} {body_text}"
        allowed, reason = outbound_scanner.gate_outbound_content(
            config=self._config, text=gate_text, surface="todo_body",
        )
        if not allowed:
            outbound_scanner.log_blocked(
                config=self._config, surface="todo_body",
                reason=f"{reason}_imessage_context",
                text=gate_text,
                extras={
                    "imessage_id": source_imessage_id,
                    "account": account or self._default_account,
                },
            )
            logger.warning(
                "graph_client.create_task_in_shared_list: BLOCKED by content "
                "scanner reason=%s imessage_id=%s",
                reason, (source_imessage_id or "")[:24],
            )
            raise OutboundContentBlocked(
                f"create_task_in_shared_list blocked: reason={reason} imessage_id={source_imessage_id}"
            )

        payload: dict[str, Any] = {
            "title": rendered_title,
            "body": {"contentType": "text", "content": body_text},
        }
        if deadline is not None:
            payload["dueDateTime"] = {
                "dateTime": deadline.replace(microsecond=0).isoformat(),
                "timeZone": "America/Los_Angeles",
            }

        resp = httpx.post(
            f"{GRAPH}/me/todo/lists/{list_id}/tasks",
            headers={**self._headers(account=account), "Content-Type": "application/json"},
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        task_id = resp.json()["id"]

        if source_imessage_id:
            try:
                httpx.post(
                    f"{GRAPH}/me/todo/lists/{list_id}/tasks/{task_id}/linkedResources",
                    headers={**self._headers(account=account), "Content-Type": "application/json"},
                    json={
                        "externalId": source_imessage_id,
                        "applicationName": "HomeOS kavi-runtime",
                        "displayName": f"iMessage: {title[:80]}",
                    },
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                logger.warning(
                    "create_task_in_shared_list: linkedResource attach failed (task created): %s", e
                )

        logger.info(
            "create_task_in_shared_list: created task_id=%s title=%r owner=%s deadline=%s account=%s",
            task_id[:12], rendered_title[:80], owner_prefix, deadline, account or self._default_account,
        )
        return task_id, True

    def update_todo_task(
        self, list_id: str, task_id: str, patch: dict[str, Any], *, account: str | None = None,
    ) -> None:
        _t0 = time.monotonic()
        _err: BaseException | None = None
        try:
            resp = httpx.patch(
                f"{GRAPH}/me/todo/lists/{list_id}/tasks/{task_id}",
                headers={**self._headers(account=account), "Content-Type": "application/json"},
                json=patch,
                timeout=30.0,
            )
            resp.raise_for_status()
        except BaseException as e:
            _err = e
            raise
        finally:
            _record_tool_call(
                "graph.update_todo_task",
                {"list_id": list_id, "task_id": task_id, "patch": patch, "account": account},
                {"ok": _err is None, "error": repr(_err) if _err else None},
                int((time.monotonic() - _t0) * 1000),
            )

    def get_todo_task(
        self, list_id: str, task_id: str, *, account: str | None = None,
    ) -> dict[str, Any]:
        resp = httpx.get(
            f"{GRAPH}/me/todo/lists/{list_id}/tasks/{task_id}",
            headers=self._headers(account=account),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    def list_recent_todo_tasks(
        self, list_id: str, top: int = 25, *, account: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent tasks in the shared list, newest first by lastModifiedDateTime.

        Status-agnostic — completed tasks are included. Use
        `list_open_todo_tasks` when the caller's intent is "tasks Megha
        could still act on" (action-layer matcher, dedup checks, summary
        rendering); recency-only callers like find_task_by_title in the
        correction path stay here because they target tasks Megha just
        saw, regardless of status.
        """
        resp = httpx.get(
            f"{GRAPH}/me/todo/lists/{list_id}/tasks",
            headers=self._headers(account=account),
            params={"$top": top, "$orderby": "lastModifiedDateTime desc"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    def list_open_todo_tasks(
        self, list_id: str, top: int = 100, *, account: str | None = None,
    ) -> list[dict[str, Any]]:
        """Open tasks in the shared list, newest first by lastModifiedDateTime.

        Server-side `$filter` excludes statuses Megha can't act on:
        - Includes: `notStarted`, `inProgress`
        - Excludes: `completed`, `waitingOnOthers`, `deferred`

        Why this exists separately from list_recent_todo_tasks:
        the recency-only fetch was capped at top=30 in the action-layer
        matcher path. On busy days, mass-completions pushed older
        notStarted tasks off that 30-row window, so the matcher honestly
        reported "no candidates" even when 3 open Elders' Tea tasks
        existed (2026-05-08 trace). Filtering server-side means the
        slate the LLM matcher sees actually represents the task universe
        Megha could be referencing.

        `top` is the most tasks returned; pages are followed via
        @odata.nextLink until it is reached. 2026-09-25: the list passed 100
        open tasks in July and the old single-page fetch silently cut off
        the OLDEST tasks, which are exactly the ones the stale-task nudge
        asks about (Kavi closed three wrong tasks on "close them").
        """
        _t0 = time.monotonic()
        _err: BaseException | None = None
        _count: int | None = None
        try:
            value: list[dict[str, Any]] = []
            url: str | None = f"{GRAPH}/me/todo/lists/{list_id}/tasks"
            params: dict[str, Any] | None = {
                "$filter": "status eq 'notStarted' or status eq 'inProgress'",
                "$top": min(top, 100),
                "$orderby": "lastModifiedDateTime desc",
            }
            while url and len(value) < top:
                resp = httpx.get(url, headers=self._headers(account=account),
                                 params=params, timeout=30.0)
                resp.raise_for_status()
                body = resp.json()
                value.extend(body.get("value", []))
                url, params = body.get("@odata.nextLink"), None
            _record_graph_read_ok("read:list_open_todo_tasks")
            value = value[:top]
            _count = len(value)
            return value
        except BaseException as e:
            _err = e
            raise
        finally:
            _record_tool_call(
                "graph.list_open_todo_tasks",
                {"list_id": list_id, "top": top, "account": account},
                {"ok": _err is None, "count": _count, "error": repr(_err) if _err else None},
                int((time.monotonic() - _t0) * 1000),
            )

    def list_todo_task_linked_resources(
        self, list_id: str, task_id: str, *, account: str | None = None,
    ) -> list[dict[str, Any]]:
        """Linked resources attached to one task (externalId carries the
        source email message id written by create_todo_task at task-creation
        time).

        Added 2026-06-10 for the evening suggest-to-close selection path:
        list_open_todo_tasks deliberately doesn't $expand=linkedResources
        (it runs on every digest fire; the expansion is only needed for the
        bounded close-suggestion candidate scan), so the close-suggestion
        selector pays one extra Graph read per scanned candidate instead.
        The scan is capped upstream (CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED in
        capabilities/kavi_persona/close_suggestions.py).
        """
        _t0 = time.monotonic()
        _err: BaseException | None = None
        _count: int | None = None
        try:
            resp = httpx.get(
                f"{GRAPH}/me/todo/lists/{list_id}/tasks/{task_id}/linkedResources",
                headers=self._headers(account=account),
                timeout=30.0,
            )
            _record_graph_read_ok("read:list_todo_task_linked_resources")
            resp.raise_for_status()
            value = resp.json().get("value", [])
            _count = len(value)
            return value
        except BaseException as e:
            _err = e
            raise
        finally:
            _record_tool_call(
                "graph.list_todo_task_linked_resources",
                {"list_id": list_id, "task_id": task_id, "account": account},
                {"ok": _err is None, "count": _count, "error": repr(_err) if _err else None},
                int((time.monotonic() - _t0) * 1000),
            )

    def list_completed_todo_tasks(
        self, list_id: str, top: int = 100, *, account: str | None = None,
    ) -> list[dict[str, Any]]:
        """Completed tasks in the shared list, newest first by completedDateTime.

        Mirror of list_open_todo_tasks but filtered to `status eq 'completed'`.
        Used by the 9 PM rollup to compute tasks_completed_today_count.

        Server-side filter + ordering keeps the call cheap and means the page
        Megha cares about (today's closures) lands at the top regardless of
        how many tasks have ever been completed in the list.
        """
        _t0 = time.monotonic()
        _err: BaseException | None = None
        _count: int | None = None
        try:
            resp = httpx.get(
                f"{GRAPH}/me/todo/lists/{list_id}/tasks",
                headers=self._headers(account=account),
                params={
                    "$filter": "status eq 'completed'",
                    "$top": top,
                    "$orderby": "completedDateTime/dateTime desc",
                },
                timeout=30.0,
            )
            _record_graph_read_ok("read:list_completed_todo_tasks")
            resp.raise_for_status()
            value = resp.json().get("value", [])
            _count = len(value)
            return value
        except BaseException as e:
            _err = e
            raise
        finally:
            _record_tool_call(
                "graph.list_completed_todo_tasks",
                {"list_id": list_id, "top": top, "account": account},
                {"ok": _err is None, "count": _count, "error": repr(_err) if _err else None},
                int((time.monotonic() - _t0) * 1000),
            )

    def send_mail(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        account: str | None = None,
        bypass_scanner: bool = False,
    ) -> None:
        """POST /me/sendMail. Plain-text body. Used for Outlook fallback when iMessage
        silent-sends (BlueBubbles Automation grant revoked). Recipient is typically
        the sending account's own outlook address; the runtime's own_outbound filter
        in handlers prevents the resulting inbox event from being processed as a task.

        Outbound-content scanner gate (added 2026-05-06 evening, A1 of audit
        follow-up): subject + body run through `gate_outbound_content` BEFORE
        the Graph POST. Same shape as the `create_todo_task` gate; raises
        `OutboundContentBlocked` on a hit.

        bypass_scanner (added 2026-05-07, Issue 2 of plain-language alert
        rewrite): when True, skip the content scanner. Reserved for runtime-
        internal alert emails sent from Megha's account back to a household
        recipient. The scanner exists to prevent leaks to outside parties;
        runtime-to-self alerts shouldn't be gated, because Python error
        messages reliably contain digit sequences that match the generic
        account-number regex and the operator never gets the heads-up.
        Caller MUST verify recipient is a household handle; otherwise raises.
        """
        from kavi_runtime.runtime import outbound_scanner
        if bypass_scanner:
            if not outbound_scanner.is_household_handle(to):
                raise OutboundContentBlocked(
                    "send_mail bypass_scanner requires household recipient; "
                    f"got to={to!r}"
                )
            logger.info(
                "graph_client.send_mail: scanner bypass to=%s subject=%s",
                (to or "")[:64], (subject or "")[:64],
            )
        else:
            gate_text = f"{subject} {body}"
            allowed, reason = outbound_scanner.gate_outbound_content(
                config=self._config, text=gate_text, surface="outbound_email",
                recipient=to,
            )
            if not allowed:
                outbound_scanner.log_blocked(
                    config=self._config, surface="outbound_email",
                    reason=f"{reason}_send_mail_context",
                    text=gate_text,
                    recipient=to,
                    extras={"account": account or self._default_account},
                )
                logger.warning(
                    "graph_client.send_mail: BLOCKED by content scanner reason=%s to=%s",
                    reason, (to or "")[:64],
                )
                raise OutboundContentBlocked(
                    f"send_mail blocked: reason={reason} to={to}"
                )

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": "true",
        }
        resp = httpx.post(
            f"{GRAPH}/me/sendMail",
            headers={**self._headers(account=account), "Content-Type": "application/json"},
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()

    def find_task_by_exact_title(
        self, list_id: str, target: str, top: int = 50, *, account: str | None = None,
    ) -> dict[str, Any] | None:
        """Stage 1 action layer: case-insensitive, trimmed exact-title match.
        Strips Kavi's title prefixes (`[?] `, owner abbrev `MJ ` / `MM `, and a
        leading `[Tag] `) from each candidate before comparing, so Megha can
        say "Oak Circle volunteering" and match a task stored as
        "MJ Oak Circle volunteering" or "[?] MJ Oak Circle volunteering".

        Returns the raw task dict from Graph (full title preserved) on a single
        match; None on zero matches or any ambiguity (multiple matches). Stage 2
        will add fuzzy matching; Stage 1 prefers a clean miss to a wrong action.
        """
        if not target:
            return None
        target_norm = target.strip().lower()
        if len(target_norm) < 3:
            return None
        tasks = self.list_recent_todo_tasks(list_id, top=top, account=account)
        matches: list[dict[str, Any]] = []
        for t in tasks:
            title = t.get("title", "") or ""
            stripped = _strip_kavi_title_prefixes(title).strip().lower()
            if stripped == target_norm:
                matches.append(t)
                continue
            if title.strip().lower() == target_norm:
                matches.append(t)
        if len(matches) == 1:
            return matches[0]
        return None

    def mark_task_done(
        self, list_id: str, task_id: str, *, account: str | None = None,
    ) -> tuple[bool, str]:
        """Stage 1 action layer: set a To Do task's status to completed."""
        self.update_todo_task(list_id, task_id, {"status": "completed"}, account=account)
        try:
            updated = self.get_todo_task(list_id, task_id, account=account)
            return True, updated.get("title", "") or ""
        except Exception as e:
            logger.warning("mark_task_done: post-PATCH get_todo_task failed: %s", e)
            return True, ""

    def find_todo_task_by_package_id(
        self, list_id: str, package_id: str, top: int = 50, *, account: str | None = None,
    ) -> str | None:
        """Tier 1 package lifecycle match: scan open tasks' body content and
        linkedResources[*].externalId for the literal `package_id` string.
        Returns the matching task ID or None.

        Filters out completed tasks (lifecycle tasks stay open until Megha
        closes manually, so a completed task represents an old order Megha
        already triaged — re-using its ID would be a regression).
        """
        if not package_id:
            return None
        resp = httpx.get(
            f"{GRAPH}/me/todo/lists/{list_id}/tasks",
            headers=self._headers(account=account),
            params={"$top": top, "$expand": "linkedResources",
                    "$orderby": "lastModifiedDateTime desc"},
            timeout=30.0,
        )
        resp.raise_for_status()
        for t in resp.json().get("value", []):
            if (t.get("status") or "").lower() == "completed":
                continue
            body_content = (t.get("body") or {}).get("content") or ""
            if package_id in body_content:
                return t["id"]
            for lr in t.get("linkedResources", []) or []:
                if lr.get("externalId") == package_id:
                    return t["id"]
        return None

    def find_todo_task_by_heuristic(
        self,
        list_id: str,
        merchant: str,
        recipient: str,
        max_age_days: int = 7,
        top: int = 50,
        *,
        account: str | None = None,
    ) -> tuple[str | None, str]:
        """Tier 2 package lifecycle match: same merchant token in body + same
        recipient + age ≤ max_age_days."""
        if not merchant:
            return None, ""
        merchant_norm = merchant.strip().lower()
        if not merchant_norm:
            return None, ""
        try:
            resp = httpx.get(
                f"{GRAPH}/me/todo/lists/{list_id}/tasks",
                headers=self._headers(account=account),
                params={"$top": top, "$orderby": "lastModifiedDateTime desc"},
                timeout=30.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("find_todo_task_by_heuristic: list query failed: %s", e)
            return None, ""

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        matches: list[tuple[str, str]] = []
        for t in resp.json().get("value", []):
            if (t.get("status") or "").lower() == "completed":
                continue
            created = t.get("createdDateTime") or t.get("lastModifiedDateTime")
            if not created:
                continue
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if created_dt < cutoff:
                continue
            body_content = ((t.get("body") or {}).get("content") or "").lower()
            title = (t.get("title") or "").lower()
            if merchant_norm not in body_content and merchant_norm not in title:
                continue
            if recipient and recipient.lower() not in body_content and recipient.lower() not in title:
                if recipient.lower() in {"megha", "mj"} and " mj " not in f" {title} " and not title.startswith("mj "):
                    continue
                if recipient.lower() in {"max", "mm"} and " mm " not in f" {title} " and not title.startswith("mm "):
                    continue
            age_days = (datetime.now(timezone.utc) - created_dt).days
            reason = f"merchant={merchant_norm}, recipient={recipient or 'any'}, gap={age_days}d"
            matches.append((t["id"], reason))

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "find_todo_task_by_heuristic: package_match_ambiguous merchant=%s n_matches=%d",
                merchant_norm, len(matches),
            )
        return None, ""

    def find_task_by_title(
        self, list_id: str, target: str, top: int = 25, *, account: str | None = None,
    ) -> dict[str, Any] | None:
        """Lowercase substring match between target and recent task titles."""
        target_norm = target.lower().strip()
        if not target_norm or len(target_norm) < 3:
            return None
        tasks = self.list_recent_todo_tasks(list_id, top=top, account=account)
        matches = []
        for t in tasks:
            title_norm = (t.get("title", "") or "").lower()
            if not title_norm:
                continue
            if target_norm in title_norm or title_norm in target_norm:
                matches.append(t)
        if len(matches) == 1:
            return matches[0]
        return None
