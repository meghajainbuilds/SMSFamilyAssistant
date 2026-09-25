"""BlueBubbles HTTP API wrapper. Send + read iMessages via Kavi's Apple ID.

The `after`/`where` query params are documented but consistently hang the API; do not
reintroduce them. Use sort=DESC + client-side filtering.

Verify-after-send: per-message receipt as ground truth (added 2026-06-03,
rewritten after the chat-poll false-negative class of bugs).

BlueBubbles' `POST /api/v1/message/text` returns a payload with a message
GUID on every successful send. That GUID is the ground truth that the
message left Kavi for Apple's iMessage layer. `send_with_verify()` now
treats the send response as the verification signal: 2xx HTTP + a GUID in
the response body means verified=True, no further polling. The Outlook
fallback is reserved exclusively for true send failure: network error,
HTTP non-2xx, missing GUID in the response, or BlueBubbles unreachable.

The chat-history polling path (`/api/v1/message/query` filtered by
tempGuid/dateDelivered) is DELETED from the verify flow. That path
depended on BlueBubbles having actively synced the recipient's chat
into its mirror, which is not the case for any new household member
(Max today, anyone added tomorrow), causing universal false-negative
"silent send" for non-Megha recipients and triggering misleading Outlook
fallbacks.

`fetch_recent_messages` remains in the client because the daily channel
heartbeat job (`bb_daily_channel_heartbeat` in
`capabilities/realtime_kavi/scheduler.py`, added 2026-06-03) uses it to
detect quiet degradation at the Apple Push layer. It MUST NOT be called
from the per-message verify flow.

Password-redaction at log time (added 2026-05-06 evening, A5 of audit follow-up):
BlueBubbles authenticates via `?password=<plaintext>` query string (no header
auth in v1.x), so httpx's INFO-level request logging was persisting the
password into kavi-runtime.err.log on every BB call. We install a module-level
logging.Filter on the `httpx` logger that rewrites any `password=<value>`
substring to `password=REDACTED` before the line is emitted. Local-only HTTP
on 127.0.0.1, but the log lives on disk and would surface via any future log
shipping integration. See `_HttpxPasswordRedactor`.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# Module-level constant: retained for backwards compatibility with the
# SILENT_SEND_DETECTED log line in `runtime/send_imessage.py`. Per-message
# verification (2026-06-03 rewrite) no longer polls, so the timeout is
# vestigial; the constant is left in place because the fallback log line
# still references it as the "how long we waited before declaring send
# failed" cap on the send_message HTTP call itself.
VERIFY_TIMEOUT_SEC = 30


# ---- httpx-log password redaction (A5) ------------------------------------

# Match `password=` followed by any non-whitespace, non-quote, non-ampersand
# token. Matches `password=foo`, `password=foo&bar`, and `password=foo "200 OK"`
# without consuming the trailing quote / ampersand. Case-insensitive.
_PASSWORD_QS_RE = re.compile(r"(?i)(password=)([^\s&\"']+)")


class _HttpxPasswordRedactor(logging.Filter):
    """Logging filter installed once on the `httpx` logger. Rewrites any
    `password=<value>` substring in the formatted log message to
    `password=REDACTED`.

    Why a filter rather than raising httpx to WARNING: keeping INFO-level
    visibility on outbound BB calls is cheap observability that paid off
    on 2026-05-04 (BB outage discovery). Dropping the level would lose
    that signal. The filter scrubs the secret without losing the request
    line. Header-auth would be cleaner but BlueBubbles v1.x does not
    accept Authorization headers; it requires the query-string param.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            try:
                msg = record.getMessage()
            except Exception:
                msg = str(record.msg)
            if "password=" in msg:
                # Replace in the rendered message, then null the args so
                # the formatter doesn't try to substitute again.
                record.msg = _PASSWORD_QS_RE.sub(r"\1REDACTED", msg)
                record.args = None
        elif isinstance(record.msg, str) and "password=" in record.msg:
            record.msg = _PASSWORD_QS_RE.sub(r"\1REDACTED", record.msg)
        return True


# Install once at import time. Idempotent: re-importing the module is a no-op
# because we keep a sentinel on the logger.
_HTTPX_LOGGER = logging.getLogger("httpx")
if not getattr(_HTTPX_LOGGER, "_kavi_password_redactor_installed", False):
    _HTTPX_LOGGER.addFilter(_HttpxPasswordRedactor())
    _HTTPX_LOGGER._kavi_password_redactor_installed = True  # type: ignore[attr-defined]


class BlueBubblesClient:
    def __init__(self, config: dict):
        self._base = config["bluebubbles"]["base_url"]
        self._password = os.environ["BLUEBUBBLES_PASSWORD"]
        self._chat_guid_prefix = config["imessage"]["chat_guid_prefix"]
        self._megha_phone = config["imessage"]["megha_phone"]

    def _params(self) -> dict[str, str]:
        return {"password": self._password}

    def _chat_guid(self) -> str:
        return f"{self._chat_guid_prefix}{self._megha_phone}"

    def chat_guid_for(self, handle: str | None) -> str:
        """Resolve the BlueBubbles chatGuid for a given iMessage handle.

        Routing rule (added 2026-05-07 after Bug 1: an addressee_reach for Max
        landed in Megha's chat because send_message hardcoded `_chat_guid()` to
        Megha's chat regardless of recipient):

          - Megha's phone → `<chat_guid_prefix><megha_phone>` (preserves the
            legacy `any;-;+1...` form for her chat — every existing send to
            Megha routes through this branch unchanged).
          - Any other handle (Max, future household members) → `iMessage;-;<handle>`.
            BlueBubbles' "address-based DM" form: routes to the 1:1 chat that
            Kavi's Apple ID already has open with that handle.
          - None / empty → falls back to Megha's chat (defensive default for
            existing callers that never pass a recipient).

        IMPORTANT (one-time chat-seed requirement): BlueBubbles can only route
        to a chat that EXISTS on Kavi's Mac. For any handle that has never
        received a message from Kavi's Apple ID before, Megha (or whoever
        operates Kavi's Mac) must open Messages.app on Kavi's MacBook and send
        ONE manual iMessage to that handle from Kavi's Apple ID. Until that
        one-time seed exists, `iMessage;-;<max_phone>`
        will resolve to a non-existent chat and BlueBubbles will silent-drop
        the send (HTTP 200, message never delivered, verify-after-send returns
        False, the Outlook fallback fires). The seed is per-handle, one-time,
        permanent — once Max's chat exists in Kavi's Messages.app, every future
        addressee_reach to Max routes correctly without intervention.
        """
        if not handle:
            return self._chat_guid()
        normalized = handle.strip()
        if normalized == self._megha_phone:
            return f"{self._chat_guid_prefix}{normalized}"
        return f"iMessage;-;{normalized}"

    def send_message(
        self,
        body: str,
        temp_guid: str,
        recipient_handle: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/message/text with chatGuid + tempGuid.

        `recipient_handle` (added 2026-05-07): when provided, the chatGuid is
        resolved via `chat_guid_for(recipient_handle)` so non-Megha addressees
        (e.g., Max) actually land in their own chat thread, not Megha's. When
        None, the legacy default of Megha's chat applies — preserving every
        existing call site that doesn't pass a recipient.

        BlueBubbles is known to hang on the response despite delivering. Treat timeout
        as conditional-success and rely on verify_send_landed() at higher level for proof.
        """
        chat_guid = self.chat_guid_for(recipient_handle) if recipient_handle else self._chat_guid()
        payload = {"chatGuid": chat_guid, "tempGuid": temp_guid, "message": body}
        try:
            resp = httpx.post(
                f"{self._base}/api/v1/message/text",
                params=self._params(),
                json=payload,
                timeout=10.0,
            )
            return {
                "status": resp.status_code,
                "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
            }
        except httpx.TimeoutException:
            return {"status": "timeout", "body": None}
        except httpx.HTTPError as e:
            return {"status": "error", "body": str(e)}

    def fetch_recent_messages(
        self,
        limit: int = 25,
        chat_guid: str | None = None,
    ) -> list[dict[str, Any]]:
        """POST /api/v1/message/query with sort=DESC, no after/where (those hang the API).

        Returns a chat's most recent messages in newest-first order. Caller
        filters client-side (e.g. by isFromMe, dateCreated, tempGuid).

        `chat_guid`: which chat to query. None defaults to Megha's chat —
        used by `bb_heartbeat` to warm BB's chat.db reads on Megha's
        primary chat.

        **LEGITIMATE CALLERS (2026-06-03 rewrite):**
          - `bb_heartbeat` (2-min chat.db keep-warm; Megha's chat).
          - `bb_daily_channel_heartbeat` (daily per-recipient round-trip
            check that detects quiet degradation at the Apple Push layer).

        **FORBIDDEN:** the per-message verify flow MUST NOT use this method.
        BlueBubbles' chat.db only mirrors chats it has actively synced, so
        polling it for a recipient who has never been mirrored returns an
        empty list and produces a false-negative "silent send" verdict.
        Per-message verification uses the message GUID returned by the send
        endpoint instead. Architectural test
        `tests/test_no_chat_poll_verify.py` enforces this.
        """
        if chat_guid is None:
            chat_guid = self._chat_guid()
        payload = {"chatGuid": chat_guid, "limit": limit, "offset": 0, "sort": "DESC"}
        try:
            resp = httpx.post(
                f"{self._base}/api/v1/message/query",
                params=self._params(),
                json=payload,
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except httpx.HTTPError as e:
            logger.warning("fetch_recent_messages failed: %s", e)
            return []

    def verify_send_landed(
        self,
        *,
        send_response: dict[str, Any],
        recipient_handle: str | None = None,
    ) -> tuple[bool, str | None]:
        """Per-message receipt verification (2026-06-03 rewrite).

        The send response from `POST /api/v1/message/text` is the ground
        truth that the message left Kavi for Apple's iMessage layer.
        A 2xx HTTP status plus a non-empty `data.guid` field means Apple
        accepted the message; no further polling is required.

        Returns (verified, message_guid). `verified` is True on a
        well-formed 2xx response with a GUID; False otherwise. The caller
        uses False as the signal to trigger the Outlook fallback — the
        underlying failure is a true send error (HTTP non-2xx, network
        failure, BlueBubbles unreachable, or BlueBubbles returned 200 but
        no GUID, all of which indicate Apple did not accept the message).

        This method DOES NOT call `fetch_recent_messages` or
        `/api/v1/message/query`. That path produced false-negative silent
        sends for any recipient whose chat had not been mirrored in
        BlueBubbles' chat.db (which is every new household member by
        construction). Architectural test
        `tests/test_no_chat_poll_verify.py` enforces this property.

        `recipient_handle` is retained on the signature for caller
        compatibility but is no longer load-bearing for verification —
        the GUID is chat-agnostic.
        """
        status = send_response.get("status")
        # True send failure shapes: timeout, error string, non-2xx HTTP.
        if not isinstance(status, int) or not (200 <= status < 300):
            return False, None

        body_resp = send_response.get("body")
        if not isinstance(body_resp, dict):
            return False, None
        data = body_resp.get("data")
        if not isinstance(data, dict):
            return False, None
        gid = data.get("guid")
        if not isinstance(gid, str) or not gid:
            return False, None
        return True, gid

    def send_with_verify(
        self,
        body: str,
        temp_guid: str | None = None,
        recipient_handle: str | None = None,
    ) -> dict[str, Any]:
        """Send + per-message receipt verification in one call.

        `recipient_handle` (added 2026-05-07): plumbed through to `send_message`
        so addressee sends (e.g., Max) route to the correct chat. None preserves
        the legacy default of Megha's chat for every existing caller that
        doesn't pass a recipient.

        Returns: {"sent": bool, "verified": bool, "temp_guid": str,
                  "message_guid": str | None, "send_response": dict}

        Caller uses `verified=False` as the signal to trigger the Outlook
        fallback (see `runtime/send_imessage.py`). Per the 2026-06-03
        rewrite, `verified=False` corresponds exclusively to a true send
        error (HTTP non-2xx, network failure, BlueBubbles unreachable, or
        BlueBubbles returned 200 with no GUID). It NEVER corresponds to
        "send succeeded but a follow-up poll couldn't find the message in
        a chat-history mirror" — that class of false negative is gone.
        """
        if temp_guid is None:
            temp_guid = f"send-{uuid.uuid4().hex[:8]}"
        send_resp = self.send_message(body, temp_guid=temp_guid, recipient_handle=recipient_handle)

        # `sent` reflects whether the HTTP call to BlueBubbles "completed"
        # (2xx or a known-benign timeout where BB sometimes hangs while
        # delivering). `verified` is the stricter signal that Apple
        # accepted the message via the returned GUID.
        status = send_resp.get("status")
        sent = (isinstance(status, int) and 200 <= status < 300) or status == "timeout"

        verified, message_guid = self.verify_send_landed(
            send_response=send_resp,
            recipient_handle=recipient_handle,
        )

        return {
            "sent": sent,
            "verified": verified,
            "temp_guid": temp_guid,
            "message_guid": message_guid,
            "send_response": send_resp,
        }
