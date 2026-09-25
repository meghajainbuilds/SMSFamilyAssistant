"""channel_heartbeat.py — passive-observation daily channel health check.

Rewritten 2026-06-04 after the 2026-06-03 synthetic-probe design re-introduced
the same broken signal class the receipt-as-truth verify rewrite (SHA
`af66b47`) had just removed. The synthetic probe sent a one-character ping
to each recipient and polled BlueBubbles' chat history for that ping —
exactly the chat-mirror-poll signal that produced false-negative silent
sends. On 2026-06-04 morning the probe false-fired two "iMessage channel
degrading" emails to Megha for her own number (while Kavi was actively
texting her) and Max's number (after Max had texted Kavi back the same
morning), because the chat-history read for Megha returned 200 OK but
didn't see the ping in the recent-50 window AND the predicate
`last_rt is None or (now - last_rt) > 24h` treated first-run identically
to 24h-of-degradation.

The architectural correction: do NOT send a synthetic probe. Observe real
traffic.

Shape:
  - Two timestamps per household recipient maintained by write-through
    hooks elsewhere in the runtime:
      * `last_outbound_receipt_at` — updated every time
        `bb.send_with_verify` returns `verified=True` AND a non-empty
        `message_guid` for that recipient. The receipt-as-truth signal
        from Layer A is the source of truth that Apple accepted a real
        outbound from Kavi to that recipient.
      * `last_inbound_at` — updated every time the BlueBubbles webhook
        delivers an inbound iMessage from that recipient handle.
    Either signal confirms the channel is healthy in at least one
    direction. Real household traffic is plentiful (Kavi messages Megha
    multiple times a day; Megha replies; Max acks coordination), so the
    24h window is not load-bearing on a synthetic probe.
  - The daily 08:00 PT cron is a pure READ. For each household recipient:
    healthy = (last_outbound_receipt_at within 24h)
           OR (last_inbound_at within 24h)
    is_new  = (now - recipient_added_at) < 72h
    if not healthy and not is_new and last_alert_sent_at is None:
        email Megha "channel degrading for <recipient>"
        set last_alert_sent_at
    elif healthy:
        clear last_alert_sent_at  # so future degradation re-alerts fresh
  - Grace period: a newly-added recipient (e.g., the migration that
    moves an existing state file to the new schema, or a future
    household member added to config) is treated as healthy for 72h
    so cold-start doesn't false-fire while real traffic seeds the
    timestamps.
  - The alert email body NO longer contains the recipient's phone
    number in a form the outbound scanner's `account_number` regex
    matches, so the prior `bypass_scanner=True` workaround is no
    longer required for this code path.

What this module does NOT do (asserted by
`tests/test_heartbeat_passive_only.py`):
  - No synthetic-send calls into the BlueBubbles client.
  - No chat-history poll calls into the BlueBubbles client.
  - No chat-resolution calls into the BlueBubbles client.
  - No `time.sleep` calls.
  - No interaction with `bluebubbles_client` at all in the daily check.

State file: `<state_dir>/channel_heartbeat.json`. Schema:

    {
      "<recipient_handle>": {
        "recipient_added_at": "<UTC ISO8601>",
        "last_outbound_receipt_at": "<UTC ISO8601> | null",
        "last_inbound_at": "<UTC ISO8601> | null",
        "last_alert_sent_at": "<UTC ISO8601> | null"
      },
      ...
    }

Migration from the old (2026-06-03) schema is automatic on first read:
the old `last_ping_temp_guid`, `last_ping_at`, `last_round_trip_at` keys
are dropped; `last_alert_sent_at` is preserved (so today's already-
dedupe'd false alerts don't re-fire if migration runs before any new
traffic is observed); `recipient_added_at` is set to now (treat migration
as fresh, since we have no historical signal to seed from).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.state_io import atomic_write_json, read_json_recover

logger = logging.getLogger(__name__)


# How stale either signal can be before we alert.
DEGRADATION_THRESHOLD_HOURS = 24

# How long after a recipient first appears in state we treat them as
# healthy regardless of signal absence. Covers cold-start after schema
# migration and the onboarding window for a freshly added household member.
RECIPIENT_GRACE_PERIOD_HOURS = 72


# ---- State path / load / save ---------------------------------------------


def _state_path(config: dict[str, Any]) -> Path:
    """Co-locate the heartbeat state with the other per-concept state files
    under `<state_dir>/channel_heartbeat.json`. We piggyback on the
    imessage_state path to discover the state directory rather than
    introducing a new config knob."""
    legacy = Path(config["paths"]["imessage_state"])
    return legacy.parent / "channel_heartbeat.json"


def _load_state(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = read_json_recover(_state_path(config), default={})
    return raw if isinstance(raw, dict) else {}


def _save_state(config: dict[str, Any], state: dict[str, dict[str, Any]]) -> None:
    atomic_write_json(_state_path(config), state)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ---- Schema migration ------------------------------------------------------


# Old-schema keys that must be dropped on migration.
_OLD_SCHEMA_KEYS_TO_DROP = ("last_ping_temp_guid", "last_ping_at", "last_round_trip_at")


def _migrate_entry_if_needed(
    entry: dict[str, Any], now_iso: str,
) -> tuple[dict[str, Any], bool]:
    """Migrate a single recipient entry from the old (2026-06-03) schema to
    the new (2026-06-04) passive-observation schema. Returns (new_entry,
    migrated). Preserves `last_alert_sent_at` so today's dedupe state
    survives the migration."""
    has_old_keys = any(k in entry for k in _OLD_SCHEMA_KEYS_TO_DROP)
    missing_new_keys = (
        "recipient_added_at" not in entry
        or "last_outbound_receipt_at" not in entry
        or "last_inbound_at" not in entry
    )
    if not (has_old_keys or missing_new_keys):
        return entry, False

    migrated: dict[str, Any] = {
        "recipient_added_at": entry.get("recipient_added_at") or now_iso,
        "last_outbound_receipt_at": entry.get("last_outbound_receipt_at"),
        "last_inbound_at": entry.get("last_inbound_at"),
        # KEEP last_alert_sent_at so today's already-dedupe'd false alerts
        # don't re-fire tomorrow if migration runs before the new check.
        "last_alert_sent_at": entry.get("last_alert_sent_at"),
    }
    return migrated, True


def _migrate_state_if_needed(
    state: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Walk every entry; migrate any that still hold the old schema."""
    now_iso = _utc_now_iso()
    any_migrated = False
    new_state: dict[str, dict[str, Any]] = {}
    for handle, entry in state.items():
        if not isinstance(entry, dict):
            new_state[handle] = entry
            continue
        migrated_entry, did_migrate = _migrate_entry_if_needed(entry, now_iso)
        new_state[handle] = migrated_entry
        if did_migrate:
            any_migrated = True
    return new_state, any_migrated


# ---- Recipient discovery ---------------------------------------------------


def discover_recipients(config: dict[str, Any]) -> list[str]:
    """Return the list of iMessage recipient handles to track.

    Sources the household from the runtime config rather than parsing
    `household.md` so a freshly-added recipient (config knob set, runtime
    restarted) starts being tracked immediately. Today: `megha_phone` plus
    `max_phone` if configured. Future household members are added one
    config knob at a time.

    Returns a deduped list, preserving config order.
    """
    imsg = config.get("imessage", {})
    seen: set[str] = set()
    recipients: list[str] = []
    for key in ("megha_phone", "max_phone"):
        h = imsg.get(key)
        if isinstance(h, str) and h and h not in seen:
            seen.add(h)
            recipients.append(h)
    return recipients


# ---- Write-through hooks (called from real-traffic code paths) -------------


def _ensure_entry(state: dict[str, dict[str, Any]], handle: str) -> dict[str, Any]:
    """Return the entry for `handle`, lazily initializing it with the new
    schema if absent. `recipient_added_at` is set to now on lazy creation
    so a future household member added to config gets a fresh grace period
    the first time they appear in a signal hook."""
    entry = state.get(handle)
    if not isinstance(entry, dict):
        entry = {
            "recipient_added_at": _utc_now_iso(),
            "last_outbound_receipt_at": None,
            "last_inbound_at": None,
            "last_alert_sent_at": None,
        }
        state[handle] = entry
    return entry


def record_outbound_receipt(config: dict[str, Any], recipient_handle: str) -> None:
    """Write-through hook: called by `runtime/send_imessage.py` whenever
    `bb.send_with_verify` returns `verified=True` AND `message_guid` is
    non-None for a household recipient. This is the receipt-as-truth
    signal that confirms Apple accepted a real outbound to the recipient.

    Failure-safe: any I/O error is logged and swallowed. A missed signal
    just means the next read sees a slightly stale `last_outbound_receipt_at`
    — the grace period and the inbound signal both cover for this.
    """
    if not recipient_handle:
        return
    try:
        state = _load_state(config)
        # Migrate first so we don't accidentally re-introduce old-schema keys.
        state, _ = _migrate_state_if_needed(state)
        entry = _ensure_entry(state, recipient_handle)
        entry["last_outbound_receipt_at"] = _utc_now_iso()
        _save_state(config, state)
    except Exception:
        logger.exception(
            "channel_heartbeat.record_outbound_receipt failed for %s "
            "(non-fatal; next signal will overwrite)",
            recipient_handle,
        )


def record_inbound(config: dict[str, Any], sender_handle: str) -> None:
    """Write-through hook: called by the BlueBubbles webhook handler
    whenever an inbound iMessage from a household recipient arrives.
    Symmetric to `record_outbound_receipt` — either signal confirms the
    channel is healthy in at least one direction within the past 24h.

    Failure-safe: same posture as `record_outbound_receipt`.
    """
    if not sender_handle:
        return
    try:
        state = _load_state(config)
        state, _ = _migrate_state_if_needed(state)
        entry = _ensure_entry(state, sender_handle)
        entry["last_inbound_at"] = _utc_now_iso()
        _save_state(config, state)
    except Exception:
        logger.exception(
            "channel_heartbeat.record_inbound failed for %s "
            "(non-fatal; next signal will overwrite)",
            sender_handle,
        )


# ---- Daily check (pure read; the only I/O is graph.send_mail on alert) -----


def run_daily_channel_heartbeat(config: dict[str, Any]) -> dict[str, Any]:
    """Cron entry point. Reads state and emails Megha if any recipient
    has been silent in BOTH directions for >24h AND has been in state
    long enough that the grace period doesn't apply.

    No iMessage send. No chat-history poll. No sleep. The only I/O is
    `graph.send_mail` when an alert is required.

    Returns a small summary dict for the test surface and for cron-tick
    observability.
    """
    from kavi_runtime.runtime import clients as _clients
    graph, _, _ = _clients._get_clients(config)

    state = _load_state(config)
    state, migrated = _migrate_state_if_needed(state)
    if migrated:
        logger.info("channel_heartbeat: migrated state from pre-2026-06-04 schema")

    recipients = discover_recipients(config)
    now = datetime.now(timezone.utc)
    degraded_threshold = timedelta(hours=DEGRADATION_THRESHOLD_HOURS)
    grace = timedelta(hours=RECIPIENT_GRACE_PERIOD_HOURS)

    summary: dict[str, Any] = {
        "recipients_probed": [],
        "healthy": [],
        "alerts_sent": [],
        "alerts_skipped_dedupe": [],
        "alerts_skipped_grace": [],
    }

    for handle in recipients:
        summary["recipients_probed"].append(handle)
        entry = _ensure_entry(state, handle)

        last_out = _parse_iso(entry.get("last_outbound_receipt_at"))
        last_in = _parse_iso(entry.get("last_inbound_at"))
        added = _parse_iso(entry.get("recipient_added_at")) or now

        healthy = (
            (last_out is not None and (now - last_out) <= degraded_threshold)
            or (last_in is not None and (now - last_in) <= degraded_threshold)
        )
        in_grace = (now - added) < grace
        last_alert = _parse_iso(entry.get("last_alert_sent_at"))
        already_alerted = last_alert is not None

        if healthy:
            summary["healthy"].append(handle)
            # Clear dedupe so the next degradation cycle re-alerts fresh.
            if already_alerted:
                entry["last_alert_sent_at"] = None
            continue

        if in_grace:
            summary["alerts_skipped_grace"].append(handle)
            continue

        if already_alerted:
            summary["alerts_skipped_dedupe"].append(handle)
            continue

        try:
            _send_degradation_alert(config, graph, handle, last_out, last_in)
            entry["last_alert_sent_at"] = _utc_now_iso()
            summary["alerts_sent"].append(handle)
        except Exception:
            logger.exception(
                "channel_heartbeat: degradation alert send failed for %s", handle,
            )

    _save_state(config, state)
    return summary


# ---- Alert email -----------------------------------------------------------


def _send_degradation_alert(
    config: dict[str, Any],
    graph,
    recipient_handle: str,
    last_outbound: datetime | None,
    last_inbound: datetime | None,
) -> None:
    """Email Megha when a recipient has been silent in both directions
    for >`DEGRADATION_THRESHOLD_HOURS`. Plain English, deterministic;
    does NOT flow through the persona/composer surface so it stays
    deliverable even when the LLM path is degraded.

    The body intentionally does NOT include the recipient's phone number
    in a standalone-digits form that would match the outbound scanner's
    `account_number` regex (10-12 standalone digits). The recipient role
    label ("Megha" / "Max") plus a friendly identifier (last 4 digits in
    parens for disambiguation) carries the same meaning without
    triggering the scanner. This removes the need for `bypass_scanner=True`
    on this code path.
    """
    own = config.get("imessage", {}).get("own_email_addresses", [])
    from kavi_runtime import household as _household
    megha_outlook = own[0] if own else _household.primary_email("megha")

    role_label = _role_label_for_handle(config, recipient_handle)
    short_id = _short_id_for_handle(recipient_handle)

    out_str = last_outbound.isoformat() if last_outbound else "never"
    in_str = last_inbound.isoformat() if last_inbound else "never"

    subject = f"[HomeOS] iMessage channel quiet for {role_label}"
    body = (
        f"Kavi has not seen any iMessage traffic in either direction with "
        f"{role_label} ({short_id}) in over {DEGRADATION_THRESHOLD_HOURS} "
        "hours.\n\n"
        f"Last outbound Kavi received receipt for: {out_str}\n"
        f"Last inbound from this recipient: {in_str}\n\n"
        "Likely causes (most common first):\n"
        "  - BlueBubbles Automation grant for Messages.app revoked on "
        "Kavi's Mac.\n"
        "  - BlueBubbles app crashed or is wedged.\n"
        "  - Apple Push delivery degraded for this recipient.\n"
        "  - No real traffic in either direction during the window "
        "(quiet day; not a true degradation).\n\n"
        "This alert will not fire again until at least one real send "
        "or receive is observed for this recipient."
    )
    graph.send_mail(megha_outlook, subject, body)
    logger.warning(
        "channel_heartbeat: degradation alert emailed to Megha for %s "
        "(last outbound: %s; last inbound: %s)",
        recipient_handle, out_str, in_str,
    )


def _role_label_for_handle(config: dict[str, Any], handle: str) -> str:
    """Return a human-readable role label for a household handle. Falls
    back to "recipient" if no config knob matches — the alert is still
    actionable because the short-id (last 4 digits) is included separately."""
    imsg = config.get("imessage", {})
    if handle and handle == imsg.get("megha_phone"):
        return "Megha"
    if handle and handle == imsg.get("max_phone"):
        return "Max"
    return "recipient"


def _short_id_for_handle(handle: str) -> str:
    """Return a non-scanner-tripping short identifier. For phone numbers,
    "ending 8737" rather than the full +1412... string. For other handles,
    return as-is (email handles don't trigger the account_number regex)."""
    if not handle:
        return "unknown"
    # Strip non-digits to test phone-ness; phone handles are E.164 ("+1" + 10 digits).
    digits = "".join(c for c in handle if c.isdigit())
    if len(digits) >= 4 and handle.startswith("+"):
        return f"ending {digits[-4:]}"
    return handle


__all__ = [
    "DEGRADATION_THRESHOLD_HOURS",
    "RECIPIENT_GRACE_PERIOD_HOURS",
    "discover_recipients",
    "record_inbound",
    "record_outbound_receipt",
    "run_daily_channel_heartbeat",
]
