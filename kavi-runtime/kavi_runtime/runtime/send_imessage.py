"""Canonical iMessage send wrapper used by every capability.

Every persona-voiced outbound flows through `_send_imessage_with_fallback`:
  1. Recipient allowlist gate (household.md handles only).
  2. Content scanner gate (block credit-card / SSN / routing / account number).
  3. Per-message receipt verification + Outlook fallback. The send-call
     response from BlueBubbles is the verification signal: 2xx + a message
     GUID means Apple accepted the message. The fallback fires only on a
     true send failure (HTTP non-2xx, network failure, BlueBubbles
     unreachable, or BlueBubbles returned 200 with no GUID) — not on a
     chat-history mirror miss, and (2026-06-03 follow-on) not on the
     documented BB-hang silent-delivery shape (`send_response.status ==
     "timeout"` with no GUID). Apple Push delivers on the BB-hang shape;
     BB just never returns the receipt. We log `BB_HANG_SILENT_DELIVERY`
     for observability and lean on the daily channel heartbeat (Layer B)
     to catch sustained degradation. See
     `bluebubbles_client.verify_send_landed`,
     `tests/test_no_chat_poll_verify.py`, and
     `tests/test_fallback_only_on_real_send_failure.py`.
  4. Eval-surface log row written to eval-persona-outbound-judgments.jsonl.

`send_imessage_raw` is a hand-coded sender (no LLM compose) used by
`handler_alerts` for the degraded-mode auto-reply when persona composition
itself failed. Still flows through the canonical wrapper so the security
gates remain in force.

`_send_imessage_with_fallback_and_context` is the action-layer flavor that
attaches a context dict to the outbound row for the Stage 1 action-layer
instrumentation. Writes a second log row tagged `_actions` so the eval
surface can join by triggered_by + ts.

Lives in `runtime/` because every capability sends iMessage.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from kavi_runtime.state import utc_now_iso
from kavi_runtime.runtime import clients as _clients_module

logger = logging.getLogger(__name__)

# Outbound provenance invariant (2026-06-10, intent-first dispatch rebuild).
# Every send names where its text came from: either an LLM compose call
# ({"llm_call": "<call_type>"}) or an audited deterministic fallback
# ({"fallback_audit": "YYYY-MM-DD"}, the cold-fallback policy's audit date).
# A send without provenance is REFUSED — blocked, logged loudly, never sent.
# This is the mechanical enforcement of the always-LLM output rule: a
# template send can no longer slip in unnoticed, because tagging it forces
# the author to either name the composer call or stamp an audit date.
_FALLBACK_AUDIT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_provenance(provenance: Any) -> str | None:
    """Return None when `provenance` is valid; otherwise a refusal reason.

    Valid: a dict with EXACTLY ONE of:
      - "llm_call": non-empty str (the composer call_type that produced the text)
      - "fallback_audit": "YYYY-MM-DD" str (audited deterministic fallback)
    """
    if not isinstance(provenance, dict):
        return "missing_provenance"
    keys = set(provenance.keys())
    if keys == {"llm_call"}:
        v = provenance.get("llm_call")
        if isinstance(v, str) and v.strip():
            return None
        return "invalid_provenance_llm_call"
    if keys == {"fallback_audit"}:
        v = provenance.get("fallback_audit")
        if isinstance(v, str) and _FALLBACK_AUDIT_DATE_RE.match(v):
            return None
        return "invalid_provenance_fallback_audit"
    return "invalid_provenance_shape"


def _get_clients(config: dict):
    """Module-local indirection so tests that monkey-patch `runtime.clients._get_clients`
    or `runtime.send_imessage._get_clients` (or `handlers._get_clients`, which
    re-exports this same name) all intercept the call. Backwards-compatible with
    pre-Phase-4 test fixtures that patched `handlers._get_clients` directly."""
    return _clients_module._get_clients(config)


def _send_imessage_with_fallback(
    config: dict,
    text: str,
    *,
    kind: str,
    provenance: dict[str, str] | None = None,
    recipient_handle: str | None = None,
    suppress_outlook_fallback: bool = False,
) -> dict[str, Any]:
    """Canonical send-iMessage wrapper. See module docstring.

    `provenance` (REQUIRED, 2026-06-10 intent-first dispatch rebuild):
    either {"llm_call": "<call_type>"} for LLM-composed text or
    {"fallback_audit": "YYYY-MM-DD"} for an audited deterministic
    fallback. A send without valid provenance is refused (blocked result,
    loud log, nothing reaches BlueBubbles). The default is None — not an
    allowance — so the refusal path, not a TypeError, is what an untagged
    legacy call site hits. The architectural test at
    tests/test_send_provenance.py scans every call site for the kwarg.

    `suppress_outlook_fallback` (2026-06-10, per-person rollup split): the
    Outlook fallback path resolves its recipient to Megha's inbox regardless
    of the iMessage addressee (known bug, tracked in backlog). Callers
    sending to anyone other than Megha (e.g. Max's rollup) pass True so a
    genuine BlueBubbles send failure logs a warning instead of misdelivering
    the message to Megha's inbox. A skipped fallback is safer than
    misdelivery.

    Returns: {"sent": bool, "verified": bool, "fallback_used": bool,
              "kind": str, "temp_guid": str, "blocked": bool,
              "blocked_reason": str | None}
    """
    # STAGING HARD BLOCK (added 2026-06-10, capability-build pipeline).
    # When config.staging_mode is true this wrapper never sends anything:
    # no BlueBubbles call, no Outlook fallback, no client construction.
    # This check is intentionally FIRST — before _get_clients, before the
    # recipient/content gates — so a staging instance can never touch a
    # real channel even if a downstream gate has a bug. Staging exists to
    # replay LLM composers (synthetic routes); all outbound is theater
    # there. See capabilities/BUILD_PIPELINE.md and config-staging.yaml.
    if config.get("staging_mode"):
        logger.warning(
            "STAGING: outbound blocked (kind=%s, recipient=%s, chars=%d)",
            kind, recipient_handle or "default", len(text or ""),
        )
        return {
            "sent": False, "verified": False, "fallback_used": False,
            "kind": kind, "temp_guid": "",
            "blocked": True, "blocked_reason": "staging_outbound_disabled",
        }

    # Gate 0: outbound provenance (2026-06-10). Refuse any send that does
    # not name its text's origin. Runs before client construction so an
    # untagged send costs nothing and touches nothing.
    provenance_problem = _validate_provenance(provenance)
    if provenance_problem is not None:
        logger.error(
            "PROVENANCE REFUSAL: outbound send blocked (kind=%s, reason=%s, "
            "text=%r). Every send must carry provenance={'llm_call': ...} "
            "or {'fallback_audit': 'YYYY-MM-DD'}.",
            kind, provenance_problem, (text or "")[:120],
        )
        try:
            from kavi_runtime.outbound_log import log_outbound
            log_outbound(
                config, kind=kind, text=text,
                send_result={
                    "sent": False, "verified": False, "fallback_used": False,
                    "blocked": True, "blocked_reason": provenance_problem,
                },
                context={"gated": True, "block_reason": provenance_problem,
                         "provenance": provenance},
            )
        except Exception as _log_err:
            logger.exception("outbound_log on provenance refusal failed: %s", _log_err)
        return {
            "sent": False, "verified": False, "fallback_used": False,
            "kind": kind, "temp_guid": "",
            "blocked": True, "blocked_reason": provenance_problem,
        }

    from kavi_runtime.runtime import outbound_scanner

    graph, _, bb = _get_clients(config)

    # Resolve the recipient handle once. Default chat target is Megha's
    # phone; coordination paths pass an explicit addressee handle (Max).
    if recipient_handle is None:
        recipient_handle = config.get("imessage", {}).get("megha_phone", "")

    # Gate 1: recipient allowlist. Blocks SEND to a handle not in
    # `household.md` before any send attempt.
    allowed_recipient, recipient_reason = outbound_scanner.gate_outbound_recipient(
        config=config, recipient=recipient_handle, text=text,
    )
    if not allowed_recipient:
        try:
            from kavi_runtime.outbound_log import log_outbound
            log_outbound(
                config, kind=kind, text=text,
                send_result={
                    "sent": False, "verified": False, "fallback_used": False,
                    "blocked": True, "blocked_reason": recipient_reason,
                },
                context={"recipient_handle": recipient_handle, "gated": True,
                         "block_reason": recipient_reason},
                recipient=recipient_handle,
            )
        except Exception as _log_err:
            logger.exception("outbound_log on blocked send failed: %s", _log_err)
        return {
            "sent": False, "verified": False, "fallback_used": False,
            "kind": kind, "temp_guid": "",
            "blocked": True, "blocked_reason": recipient_reason,
        }

    # Gate 2: content scanner. Blocks sensitive-pattern hits.
    allowed_content, content_reason = outbound_scanner.gate_outbound_content(
        config=config, text=text, surface="imessage",
        recipient=recipient_handle,
    )
    if not allowed_content:
        try:
            from kavi_runtime.outbound_log import log_outbound
            log_outbound(
                config, kind=kind, text=text,
                send_result={
                    "sent": False, "verified": False, "fallback_used": False,
                    "blocked": True, "blocked_reason": content_reason,
                },
                context={"recipient_handle": recipient_handle, "gated": True,
                         "block_reason": content_reason},
                recipient=recipient_handle,
            )
        except Exception as _log_err:
            logger.exception("outbound_log on blocked send failed: %s", _log_err)
        return {
            "sent": False, "verified": False, "fallback_used": False,
            "kind": kind, "temp_guid": "",
            "blocked": True, "blocked_reason": content_reason,
        }

    temp_guid = f"{kind}-{uuid.uuid4().hex[:8]}"

    try:
        result = bb.send_with_verify(
            text, temp_guid=temp_guid, recipient_handle=recipient_handle,
        )
    except Exception as e:
        logger.warning("imessage send_with_verify raised (kind=%s): %s", kind, e)
        result = {"sent": False, "verified": False, "temp_guid": temp_guid}

    fallback_used = False

    # Channel heartbeat write-through (added 2026-06-04). When the per-message
    # receipt confirms Apple accepted this outbound for a household recipient,
    # update `channel_heartbeat.last_outbound_receipt_at` for the recipient.
    # The daily 08:00 PT heartbeat reads this timestamp (along with the
    # inbound-webhook timestamp) to decide whether the channel is healthy.
    # Passive observation of real traffic replaces the prior synthetic ping
    # + chat-history poll, which re-introduced the same false-negative class
    # the receipt-as-truth verify rewrite (SHA af66b47) had just removed.
    if result.get("verified") and result.get("message_guid"):
        try:
            from kavi_runtime.runtime import outbound_scanner as _scanner
            if _scanner.is_household_handle(recipient_handle):
                from kavi_runtime import channel_heartbeat as _hb
                _hb.record_outbound_receipt(config, recipient_handle)
        except Exception:
            logger.debug(
                "channel_heartbeat.record_outbound_receipt emit failed (continuing)"
            )

    if not result.get("verified"):
        # Per-message receipt verification failed. Three possible shapes:
        #
        #   (A) GENUINE send failure — HTTP non-2xx, network error, BB
        #       unreachable, or BB returned 200 with no GUID. The message
        #       did NOT leave Kavi for Apple's iMessage layer. Outlook
        #       fallback is correct here: Megha needs to know.
        #
        #   (B) KNOWN BB-HANG silent delivery — BB returns
        #       `send_response.status == "timeout"` with no GUID for chats
        #       that aren't actively mirrored in its local chat.db.
        #       Documented in `bluebubbles_client.send_message`. Apple Push
        #       still delivers the message; BB just never returns the
        #       receipt. Triggering an Outlook fallback here false-alarms
        #       Megha about a send that actually landed. Suppress the
        #       fallback and log a structured `BB_HANG_SILENT_DELIVERY`
        #       event for observability instead. The daily channel
        #       heartbeat (Layer B) is the safety net for sustained
        #       degradation — per-message alerts now mean "we are
        #       confident the send failed."
        #
        #   (C) Any other unknown shape — treat as (A) for safety.
        #
        # Architectural test: `tests/test_fallback_only_on_real_send_failure.py`.
        send_response = result.get("send_response") if isinstance(result, dict) else None
        send_status = (send_response or {}).get("status") if isinstance(send_response, dict) else "unknown"
        message_guid = result.get("message_guid") if isinstance(result, dict) else None
        is_bb_hang = (send_status == "timeout") and (message_guid is None)

        if is_bb_hang:
            # Shape (B): BB-hang silent delivery. Do NOT email Megha; log
            # for observability and rely on the daily channel heartbeat
            # to catch sustained degradation.
            logger.info(
                "BB_HANG_SILENT_DELIVERY: BlueBubbles returned timeout with no GUID "
                "(kind=%s, temp_guid=%s, recipient=%s). Apple Push almost certainly "
                "delivered; suppressing per-message Outlook fallback. Daily channel "
                "heartbeat covers sustained degradation.",
                kind, temp_guid, recipient_handle,
            )
            try:
                from kavi_runtime.structured_log import log_event
                log_event(
                    "outbound", "BB_HANG_SILENT_DELIVERY",
                    recipient=recipient_handle, kind=kind,
                    temp_guid=temp_guid,
                )
            except Exception:
                logger.debug("structured_log BB_HANG_SILENT_DELIVERY emit failed (continuing)")
        elif suppress_outlook_fallback:
            # Shape (A) / (C) but the caller forbade the Outlook fallback —
            # the fallback path delivers to Megha's inbox regardless of the
            # iMessage addressee (known recipient-resolution bug, in
            # backlog), so falling back here would misdeliver. Log loudly
            # and stay silent on the email channel.
            logger.warning(
                "imessage_send_failed: BlueBubbles did not return a "
                "confirming response (kind=%s, temp_guid=%s, status=%s, "
                "recipient=%s). Outlook fallback SUPPRESSED by caller — the "
                "fallback path would land in Megha's inbox, not the "
                "addressee's. Message NOT delivered.",
                kind, temp_guid, send_status, recipient_handle,
            )
        else:
            # Shape (A) / (C): genuine send failure. Email Megha.
            logger.warning(
                "imessage_send_failed: BlueBubbles did not return a confirming response "
                "(kind=%s, temp_guid=%s, status=%s). Falling back to Outlook.",
                kind, temp_guid, send_status,
            )
            try:
                own = config.get("imessage", {}).get("own_email_addresses", [])
                from kavi_runtime import household as _household
                megha_outlook = own[0] if own else _household.primary_email("megha")
                subject = f"[HomeOS Q] {text[:60]}"
                body = (
                    "iMessage send failed (BlueBubbles did not confirm the message "
                    "with a GUID — likely BlueBubbles unreachable, Automation grant "
                    "revoked on Kavi's Mac, or a network failure). Falling back to "
                    "email so you don't miss this.\n\n"
                    "Original message Kavi tried to send:\n"
                    "---\n"
                    f"{text}\n"
                    "---\n\n"
                    f"Sent at: {utc_now_iso()}\n"
                    f"Kind: {kind}"
                )
                graph.send_mail(megha_outlook, subject, body)
                fallback_used = True
                logger.info("outlook_fallback_sent: kind=%s", kind)
            except Exception as e:
                logger.exception(
                    "outlook_fallback_failed: kind=%s err=%s. Megha will NOT see this message.",
                    kind, e,
                )

    send_result = {
        "sent": result.get("sent", False),
        "verified": result.get("verified", False),
        "fallback_used": fallback_used,
        "kind": kind,
        "temp_guid": temp_guid,
        "blocked": False,
        "blocked_reason": None,
    }

    try:
        from kavi_runtime.outbound_log import log_outbound
        log_outbound(
            config, kind=kind, text=text, send_result=send_result,
            context={"recipient_handle": recipient_handle, "gated": False,
                     # Eval rows stamp the provenance (2026-06-10): every
                     # outbound row names the LLM call (or audit date) its
                     # text came from.
                     "provenance": provenance},
            recipient=recipient_handle,
        )
    except Exception as _log_err:
        logger.exception("outbound_log auto-log failed (non-fatal): %s", _log_err)

    try:
        from kavi_runtime.structured_log import log_event
        log_event(
            "outbound", "imessage_sent",
            recipient=recipient_handle, kind=kind,
            char_count=len(text or ""),
            verified=bool(send_result.get("verified")),
            fallback_used=bool(send_result.get("fallback_used")),
        )
    except Exception:
        logger.debug("structured_log imessage_sent emit failed (continuing)")

    return send_result


def send_imessage_raw(
    config: dict, text: str, *, recipient_handle: str,
) -> dict[str, Any]:
    """Hand-coded iMessage send that bypasses persona composition entirely.

    Used by `handler_alerts._send_fallback_imessage` for the degraded-mode
    auto-reply when persona composition itself failed. No LLM call. Still
    flows through the canonical send wrapper so the recipient allowlist
    and content-scanner gates run on every fallback (defense in depth —
    even a hand-coded text must not bypass the security gates).

    Returns the same shape as `_send_imessage_with_fallback` so callers
    can assert on `sent` / `verified` / `blocked` without special-casing.

    Provenance: the degraded-mode auto-reply text is the audited
    deterministic fallback class by definition — this wrapper stamps
    fallback_audit itself so its callers (handler_alerts) need no tag.
    AUDIT 2026-06-10: honest "I'm degraded" sentence, no action claims.
    """
    return _send_imessage_with_fallback(
        config, text, kind="alert_fallback", recipient_handle=recipient_handle,
        provenance={"fallback_audit": "2026-06-10"},
    )


def _send_imessage_with_fallback_and_context(
    config: dict, text: str, *, kind: str, context: dict[str, Any],
    provenance: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Wraps `_send_imessage_with_fallback` so callers that need to attach a
    `context` dict to the outbound row (Stage 1 action-layer instrumentation)
    don't have to bypass the verify-after-send + Outlook fallback path.

    Implementation: the standard send path already auto-logs the outbound row
    with no context. To attach context without double-logging, we (1) call the
    standard send path, then (2) append a follow-up row solely for the action
    instrumentation. NOTE: this writes a SECOND row tagged with kind=
    `<kind>_actions` for the eval surface to join on. Cheap, zero
    risk of dropping the original outbound, and avoids reshuffling
    `_send_imessage_with_fallback`'s signature for one Stage 1 caller.
    """
    send_result = _send_imessage_with_fallback(
        config, text, kind=kind, provenance=provenance,
    )
    try:
        from kavi_runtime.outbound_log import log_outbound
        log_outbound(
            config,
            kind=f"{kind}_actions",
            text="",  # the actual text was already logged by the standard send path
            send_result=send_result,
            context=context,
        )
    except Exception as e:
        logger.warning("action-context outbound log failed (non-fatal): %s", e)
    return send_result


__all__ = [
    "_send_imessage_with_fallback",
    "send_imessage_raw",
    "_send_imessage_with_fallback_and_context",
]
