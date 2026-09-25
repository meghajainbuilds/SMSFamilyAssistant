"""Runtime health probes.

Two complementary checks:

1. `ping_healthcheck` — outbound dead-man heartbeat to healthchecks.io.
   When the runtime stops sending heartbeats, healthchecks.io emails Megha
   within ~15 min. Catches "process crashed / Mac asleep" silent failures.

2. `funnel_reachability_check` — inbound public-URL probe through Tailscale
   Funnel. The runtime hits its own public URL; the request takes the same
   path Microsoft Graph webhooks take (Internet → Funnel → loopback :8080
   → runtime). Catches "process up but Funnel/loopback link broken" — exactly
   today's 2026-05-06 PM root cause where server.host was bound to the
   Tailscale interface but Funnel forwards to 127.0.0.1.

Both run from the scheduler. Both are best-effort; failures log + return,
never raise (a failed health probe must not crash the scheduler).
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)


def healthcheck_url(config: dict) -> str | None:
    """Return the configured ping URL, or None if disabled."""
    return (config.get("healthcheck") or {}).get("url")


def ping_healthcheck(config: dict) -> None:
    """5-minute interval job. GETs the healthcheck ping URL with a 10s timeout.
    On 200 → debug-log success. On non-200 / timeout / network error → warn and
    return; never raise (a failed heartbeat must not crash the scheduler)."""
    url = healthcheck_url(config)
    if not url:
        # Should not happen — scheduler skips registration when URL is missing —
        # but guard anyway in case config is reloaded with the slot cleared.
        return
    try:
        resp = httpx.get(url, timeout=10.0)
    except httpx.TimeoutException:
        logger.warning("healthcheck ping timed out after 10s")
        return
    except httpx.HTTPError as e:
        logger.warning("healthcheck ping failed: %s", e)
        return
    if resp.status_code == 200:
        logger.debug("healthcheck ping ok")
    else:
        logger.warning("healthcheck ping returned %d", resp.status_code)


# ---- Funnel reachability check --------------------------------------------
#
# Catches today's 2026-05-06 PM root cause class: the runtime's bind address
# and Tailscale Funnel's forward target drift out of sync, and Microsoft gets
# 502 BadGateway on every webhook delivery while /health still returns 200
# locally. The check probes the runtime's OWN public URL (via Tailscale
# Funnel) and reports degraded health if the round-trip fails.

_FUNNEL_LOCK = threading.Lock()
_funnel_reachable: bool = True  # optimistic default — first check verifies
_last_funnel_check_at: float | None = None
_consecutive_funnel_failures: int = 0

# Threshold for transitioning to degraded — one bad probe could be a transient
# Tailscale relay blip. Two in a row (over 30 min) is durable enough to alarm.
FUNNEL_FAILURE_THRESHOLD = 2


def funnel_public_url(config: dict) -> str | None:
    """Return the configured public URL the runtime is exposed at, or None."""
    return (config.get("server") or {}).get("public_url")


def funnel_reachability_check(config: dict) -> dict:
    """15-min interval job. GET <public_url>/health and verify the response
    matches what the local runtime serves. Updates the module-level
    `_funnel_reachable` flag.

    Treats 200 (healthy) AND 503 (degraded with body) as 'Funnel link works'
    — both prove that Tailscale → loopback → runtime is intact. Only network
    errors, timeouts, and 502/504/etc proxy errors flip to unreachable.

    Returns a dict describing what happened. NEVER raises.
    """
    global _funnel_reachable, _last_funnel_check_at, _consecutive_funnel_failures
    result: dict = {"reachable": None, "status_code": None, "skipped_reason": None}

    public_url = funnel_public_url(config)
    if not public_url:
        result["skipped_reason"] = "no_public_url_configured"
        return result

    probe_url = public_url.rstrip("/") + "/health"
    try:
        resp = httpx.get(probe_url, timeout=10.0)
        status = resp.status_code
        result["status_code"] = status
        # 200 = healthy locally; 503 = invariants degraded but the Funnel link
        # itself is intact (we got a response from the runtime through the
        # tunnel). Both prove reachability.
        reachable = status in (200, 503)
    except httpx.TimeoutException:
        logger.warning("funnel_reachability_check: timeout probing %s", probe_url)
        reachable = False
        result["status_code"] = "timeout"
    except httpx.HTTPError as e:
        logger.warning("funnel_reachability_check: %s probing %s", e, probe_url)
        reachable = False
        result["status_code"] = "http_error"

    result["reachable"] = reachable
    now = time.time()

    with _FUNNEL_LOCK:
        _last_funnel_check_at = now
        was_reachable = _funnel_reachable
        if reachable:
            _consecutive_funnel_failures = 0
            _funnel_reachable = True
            if not was_reachable:
                logger.info(
                    "funnel_reachability_check: RESTORED — %s returned %s",
                    probe_url, result["status_code"],
                )
        else:
            _consecutive_funnel_failures += 1
            # Only flip to unreachable after the threshold; one transient blip
            # is not worth alarming on.
            if _consecutive_funnel_failures >= FUNNEL_FAILURE_THRESHOLD:
                _funnel_reachable = False
                if was_reachable:
                    logger.critical(
                        "funnel_reachability_check: DEGRADED — %s returned %s "
                        "for %d consecutive checks. Microsoft webhook deliveries "
                        "likely returning 502 BadGateway. Verify "
                        "Tailscale Funnel target matches server.host bind "
                        "(`tailscale serve status` should forward to a port "
                        "the runtime is listening on).",
                        probe_url, result["status_code"], _consecutive_funnel_failures,
                    )
            else:
                logger.warning(
                    "funnel_reachability_check: probe %d/%d failed for %s (%s); "
                    "not yet degraded — one more consecutive failure will flip /health",
                    _consecutive_funnel_failures, FUNNEL_FAILURE_THRESHOLD,
                    probe_url, result["status_code"],
                )
    return result


def is_funnel_reachable() -> bool:
    """Read the latest reachability flag. Used by /health to surface
    Funnel-side outages alongside boot-time invariants."""
    with _FUNNEL_LOCK:
        return _funnel_reachable


def reset_funnel_state_for_test() -> None:
    """Test-only: reset the in-memory flag so tests start from a clean slate."""
    global _funnel_reachable, _last_funnel_check_at, _consecutive_funnel_failures
    with _FUNNEL_LOCK:
        _funnel_reachable = True
        _last_funnel_check_at = None
        _consecutive_funnel_failures = 0


# ---- Post-restart E2E smoke test ------------------------------------------
#
# Closes P1.4. Once at startup (after a 30-sec delay), POST a synthetic Graph
# webhook notification through the runtime's own public URL with a sentinel
# message_id. The runtime accepts the webhook, fires email_arrived async,
# fetch_message returns None on 404 (P3.10), handler returns
# {"status": "skipped", "reason": "stale_notification_404"} cleanly.
#
# Success criterion: the POST returns 202 Accepted within the timeout. That
# proves Funnel → loopback → /graph/notifications → subscription_id routing
# all work. The deeper handler chain is exercised but not directly verified
# (would require log scraping, deferred).

SMOKE_TEST_SUBSCRIPTION_HINT = "smoke-test"


def _build_synthetic_notification(subscription_id: str, client_state: str) -> dict:
    """Construct a synthetic Graph webhook notification payload using a real
    subscription_id (so the runtime's index accepts it) but a sentinel
    message_id (so fetch_message 404s and the handler skips cleanly)."""
    import uuid as _uuid
    from kavi_runtime.graph_client import SMOKE_TEST_MESSAGE_ID_PREFIX
    # Shared constant (2026-06-10): fetch_message recognizes this prefix
    # and skips cleanly on the 400 Graph returns for the malformed id —
    # previously every restart logged one ERROR traceback for it.
    sentinel_id = f"{SMOKE_TEST_MESSAGE_ID_PREFIX}{_uuid.uuid4().hex[:12]}"
    return {
        "value": [{
            "subscriptionId": subscription_id,
            "clientState": client_state,
            "changeType": "created",
            "resource": f"/me/messages/{sentinel_id}",
            "resourceData": {"id": sentinel_id, "@odata.type": "#Microsoft.Graph.Message"},
            "tenantId": SMOKE_TEST_SUBSCRIPTION_HINT,
        }]
    }


def runtime_smoke_test(config: dict) -> dict:
    """Once-at-startup probe that exercises the full inbound webhook chain.
    Reads default-account subscription state, POSTs a synthetic notification
    to the public URL, expects 202 Accepted. Logs CRITICAL on failure.

    Skipped cleanly when public_url, subscription_state, or required fields
    are missing — those are state_invariants concerns, not smoke-test
    concerns. NEVER raises.

    Returns a dict describing what happened. Used by tests + scheduler.
    """
    result: dict = {"status": None, "skipped_reason": None}

    public_url = funnel_public_url(config)
    if not public_url:
        result["skipped_reason"] = "no_public_url"
        return result

    # Lazy imports to avoid circulars at module load time.
    try:
        from kavi_runtime.graph_client import GraphClient, discover_household_accounts
        accounts = discover_household_accounts(config)
        if not accounts:
            result["skipped_reason"] = "no_accounts_configured"
            return result
        # Read subscription state for the default account (no GraphClient
        # instantiation needed; just file read).
        from kavi_runtime.graph_client import subscription_state_path_for
        sub_path = subscription_state_path_for(accounts[0])
        if not sub_path.exists():
            result["skipped_reason"] = "no_subscription_state"
            return result
        import json as _json
        sub_state = _json.loads(sub_path.read_text())
        sub_id = sub_state.get("subscription_id")
        client_state = sub_state.get("client_state")
        if not sub_id or not client_state:
            result["skipped_reason"] = "subscription_state_incomplete"
            return result
    except Exception as e:
        logger.warning("runtime_smoke_test: setup error (%s); skipping", e)
        result["skipped_reason"] = f"setup_error: {e}"
        return result

    notification = _build_synthetic_notification(sub_id, client_state)
    probe_url = public_url.rstrip("/") + "/graph/notifications"
    try:
        resp = httpx.post(probe_url, json=notification, timeout=30.0)
        result["status"] = resp.status_code
    except httpx.TimeoutException:
        logger.critical(
            "runtime_smoke_test: TIMEOUT after 30s posting to %s. Inbound webhook "
            "chain is broken end-to-end. Investigate Funnel + runtime listener.",
            probe_url,
        )
        result["status"] = "timeout"
        return result
    except httpx.HTTPError as e:
        logger.critical(
            "runtime_smoke_test: %s posting to %s. Inbound webhook chain is "
            "broken end-to-end. Investigate Funnel + runtime listener.",
            e, probe_url,
        )
        result["status"] = "http_error"
        return result

    if resp.status_code == 202:
        logger.info(
            "runtime_smoke_test: PASS — synthetic notification accepted by "
            "runtime through Funnel (%s).", probe_url,
        )
    else:
        logger.critical(
            "runtime_smoke_test: FAIL — %s returned status %d on synthetic "
            "notification POST. Inbound webhook chain may be broken.",
            probe_url, resp.status_code,
        )
    return result
