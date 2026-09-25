"""state_invariants.py — startup self-check for the runtime.

Why: 2026-05-06 silent-failure. After a secrets/subscriptions migration the
runtime came up with `/health` returning 200, the scheduler ticking, and
BlueBubbles heartbeats green — but `~/.config/kavi/subscriptions/` was
empty for the only active account. No webhook handler was ever invoked
because the runtime had no record of which subscription belonged to which
account. Megha lost ~4 hours of inbound traffic before noticing.

This module enforces the invariants that "process up" should imply, and
exposes the result via `/health` so the runtime reports itself degraded
rather than silently broken.

Per-account invariants checked at boot:
  1. Token cache file exists at `tokens/<account>.json`.
  2. Subscription state file exists at `subscriptions/<account>.json`
     with a usable `subscription_id` and `expiration_dt > now + 1h`.

A CRITICAL violation flips `/health` to 503 and prints the exact command
Megha needs to run to fix it (currently always:
`python -m kavi_runtime.add_account <account>`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from kavi_runtime.graph_client import (
    subscription_state_path_for,
    token_cache_path_for,
)

logger = logging.getLogger(__name__)

# How close to expiry counts as "we should already have renewed by now."
SUBSCRIPTION_EXPIRY_BUFFER_MINUTES = 60

# Legacy state file locations that the runtime no longer reads from.
# A file present at one of these paths is an artifact of a partial
# migration — it should be moved to the new canonical location
# (HomeOS/state/) or archived. Detected at startup; logged CRITICAL but
# does not flip /health to 503 (the runtime is still healthy with the
# new path; this is a tidiness alarm).
LEGACY_RUNTIME_STATE_PATHS: list[Path] = [
    Path.home() / "HomeOS" / ".claude" / "imessage-state.json",
    Path("/Users/kavi/HomeOS/.claude/imessage-state.json"),
    Path("/Users/kavi/kavi-runtime/state/imessage-state.json"),
]


class Violation(NamedTuple):
    severity: str  # "CRITICAL" | "WARNING"
    account: str | None
    message: str
    fix_command: str | None


def check_legacy_state_paths(
    legacy_paths: list[Path] | None = None,
) -> list[Violation]:
    """Detect runtime state files at deprecated locations. Returns a list
    of Violation (severity=WARNING — these don't flip /health to 503;
    the runtime is healthy with the canonical path. They surface in the
    err log + daily error-budget email so Megha sees the leftover and
    cleans up).

    Closes P1.6 from kavi-runtime/audits/architecture_audit_2026-05-06.md.
    """
    paths = legacy_paths if legacy_paths is not None else LEGACY_RUNTIME_STATE_PATHS
    violations: list[Violation] = []
    for legacy in paths:
        if legacy.exists():
            violations.append(Violation(
                severity="WARNING",
                account=None,
                message=(
                    f"legacy state file at {legacy} — runtime now reads "
                    f"from HomeOS/state/. Move or archive."
                ),
                fix_command=(
                    f"mv {legacy} {legacy}.legacy-archived "
                    f"(or rsync into HomeOS/state/ then delete)"
                ),
            ))
    return violations


def check_startup_invariants(
    accounts: list[str],
    *,
    now: datetime | None = None,
    include_legacy_state_check: bool = True,
) -> list[Violation]:
    """Walk every configured account; assert its on-disk state is consistent.

    Returns a list of Violation. Empty list = healthy.

    Pure function: no side effects, no logging. The caller decides what to
    do with the violations (log them, expose via /health, etc.).

    `include_legacy_state_check`: when True (default), also runs the
    P1.6 legacy state path detection. Disabled in tests that don't care
    about the host filesystem.
    """
    now = now or datetime.now(timezone.utc)
    violations: list[Violation] = []

    if include_legacy_state_check:
        violations.extend(check_legacy_state_paths())

    for account in accounts:
        token_path = token_cache_path_for(account)
        sub_path = subscription_state_path_for(account)
        token_exists = token_path.exists()
        sub_exists = sub_path.exists()

        # 2026-05-07 Issue 3: distinguish "configured but never onboarded"
        # from "configured + onboarded + something is broken." When NEITHER
        # token nor subscription file exists, the household member is in
        # household.md (so we know they exist) but add_account.py has never
        # been run for them. That's a known-and-tracked state, not a runtime
        # outage — emit WARNING so /health stays 200 instead of sitting
        # lit-red until onboarding happens. Once a token OR subscription
        # appears, partial state is treated as CRITICAL (something started
        # onboarding then failed; that IS broken).
        if not token_exists and not sub_exists:
            violations.append(Violation(
                severity="WARNING",
                account=account,
                message=(
                    f"{account} is configured in household.md but not yet "
                    f"onboarded (no tokens, no subscription on disk)"
                ),
                fix_command=f".venv/bin/python -m kavi_runtime.add_account {account}",
            ))
            continue

        if not token_exists:
            violations.append(Violation(
                severity="CRITICAL",
                account=account,
                message=f"token cache missing at {token_path}",
                fix_command=f".venv/bin/python -m kavi_runtime.add_account {account}",
            ))
            # Skip the subscription check; it'll fail too and we already
            # know what command to run.
            continue

        if not sub_exists:
            violations.append(Violation(
                severity="CRITICAL",
                account=account,
                message=f"subscription state missing at {sub_path}",
                fix_command=f".venv/bin/python -m kavi_runtime.add_account {account}",
            ))
            continue

        try:
            import json
            sub_state = json.loads(sub_path.read_text())
        except Exception as exc:
            violations.append(Violation(
                severity="CRITICAL",
                account=account,
                message=f"subscription state at {sub_path} unreadable: {exc}",
                fix_command=f".venv/bin/python -m kavi_runtime.add_account {account}",
            ))
            continue

        if "subscription_id" not in sub_state or "expiration_dt" not in sub_state:
            violations.append(Violation(
                severity="CRITICAL",
                account=account,
                message=f"subscription state at {sub_path} missing required keys",
                fix_command=f".venv/bin/python -m kavi_runtime.add_account {account}",
            ))
            continue

        try:
            expires = datetime.fromisoformat(
                sub_state["expiration_dt"].replace("Z", "+00:00")
            )
        except (ValueError, AttributeError) as exc:
            violations.append(Violation(
                severity="CRITICAL",
                account=account,
                message=f"subscription expiration_dt unparseable for {account}: {exc}",
                fix_command=f".venv/bin/python -m kavi_runtime.add_account {account}",
            ))
            continue

        buffer = timedelta(minutes=SUBSCRIPTION_EXPIRY_BUFFER_MINUTES)
        if expires < now:
            violations.append(Violation(
                severity="CRITICAL",
                account=account,
                message=(
                    f"subscription for {account} expired at {expires.isoformat()} "
                    f"(now={now.isoformat()})"
                ),
                fix_command=f".venv/bin/python -m kavi_runtime.add_account {account}",
            ))
        elif expires < now + buffer:
            # Within the renewal buffer — the scheduler should be renewing
            # right around now. Flag as WARNING so we notice if the renewer
            # is wedged, without flipping /health to 503.
            violations.append(Violation(
                severity="WARNING",
                account=account,
                message=(
                    f"subscription for {account} expires at {expires.isoformat()} "
                    f"(within {buffer} of now); renewal expected imminently"
                ),
                fix_command=None,
            ))

    return violations


def log_startup_invariants(violations: list[Violation]) -> None:
    """Log violations at the right severity. Side-effecting; call from build_app."""
    if not violations:
        logger.info("state_invariants: all checks pass")
        return
    for v in violations:
        log_fn = logger.critical if v.severity == "CRITICAL" else logger.warning
        if v.fix_command:
            log_fn("state_invariants: %s -- fix: %s", v.message, v.fix_command)
        else:
            log_fn("state_invariants: %s", v.message)
    critical_count = sum(1 for v in violations if v.severity == "CRITICAL")
    if critical_count:
        logger.critical(
            "state_invariants: %d CRITICAL violation(s); /health will report degraded",
            critical_count,
        )
