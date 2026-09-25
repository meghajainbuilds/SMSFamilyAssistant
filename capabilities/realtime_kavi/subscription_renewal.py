"""realtime-kavi subscription renewal — keeps MS Graph mail webhooks alive.

The scheduler fires `subscription_renewal_check_all_accounts` every N
minutes; for each household account it calls
`graph.renew_subscription_if_needed`, which is idempotent and a no-op when
the current subscription has >24h life left. Per-account failures are
logged and swallowed so one stuck account doesn't block renewal on the
others.

Lives in `capabilities/realtime_kavi/` because subscription renewal IS
the live-loop heartbeat for the runtime capability — without it, the
Graph webhook silently expires and inbox-to-task stops receiving email.
"""

from __future__ import annotations

import logging

from kavi_runtime.runtime.clients import _get_clients

logger = logging.getLogger(__name__)


def subscription_renewal_check(config: dict, account: str | None = None) -> None:
    """Single-account renewal helper. Kept for backward compatibility with
    any external callers and tests. Delegates to graph_client; logs and
    swallows errors so a transient network blip doesn't crash the
    scheduler."""
    graph, _, _ = _get_clients(config)
    try:
        graph.renew_subscription_if_needed(account=account)
    except Exception as e:
        logger.warning(
            "subscription_renewal_check failed account=%s: %s",
            account or "default", e,
        )


def subscription_renewal_check_all_accounts(config: dict) -> None:
    """Scheduler entry point for MS Graph subscription renewal across every
    configured household account. Iterates each account; per-account
    failures are logged and swallowed so one stuck account doesn't block
    renewal on the others. Accounts whose token has not yet been added
    are skipped at the graph_client layer (it logs and returns)."""
    graph, _, _ = _get_clients(config)
    for account in graph.accounts():
        try:
            graph.renew_subscription_if_needed(account=account)
        except Exception as e:
            logger.warning(
                "subscription_renewal_check failed account=%s: %s", account, e,
            )


__all__ = [
    "subscription_renewal_check",
    "subscription_renewal_check_all_accounts",
]
