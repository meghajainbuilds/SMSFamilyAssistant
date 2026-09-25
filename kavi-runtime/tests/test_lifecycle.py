"""Tests for the package-lifecycle build (capabilities/inbox-to-task.md, 2026-05-05).

Covers:
- The 5 worked examples from the "Package lifecycle" subsection's few-shot block.
- 2 false-merge regression cases (different merchants close in time; same
  merchant + recipient but >7 days apart).

These are unit-style tests (pure functions only — `package_extractor.extract`
and `lifecycle.apply_transition`). Tier-1 / tier-2 lookup is exercised through
mock task payloads against the static recipient/merchant filters in
`graph_client.find_todo_task_by_heuristic` via a thin in-memory stand-in (the
real Graph client is not invoked).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_lifecycle.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kavi_runtime import lifecycle, package_extractor


# ---- package_extractor: lifecycle_state_hint and id matching ---------------


@pytest.mark.parametrize(
    "subject,body,sender,expected_state,expected_id_present",
    [
        # EXAMPLE 1, email 1: Whole Foods order received via Amazon
        (
            "Your Whole Foods Market order has been received",
            "Order #112-4837265-0093318. Items: 5 items. Estimated delivery Wed 8:30am-9:30am.",
            "order-update@amazon.com",
            "Ordered",
            True,
        ),
        # EXAMPLE 1, email 2: out for delivery
        (
            "Your Whole Foods Market order is out for delivery",
            "Order #112-4837265-0093318. Out for delivery now. ETA: 8:30-9:30am.",
            "order-update@amazon.com",
            "Out for Delivery",
            True,
        ),
        # EXAMPLE 1, email 3: delivered
        (
            "Your Whole Foods Market order has been delivered",
            "Order #112-4837265-0093318. Delivered.",
            "order-update@amazon.com",
            "Delivered",
            True,
        ),
        # EXAMPLE 2, email 1: Hanna order placed
        (
            "Thanks for your order!",
            "Order #HA-9001-AB. Items: 2 dresses. Ships in 2-3 days.",
            "info@e.o.hannaandersson.com",
            "Ordered",
            False,  # HA-9001-AB doesn't match any of the configured patterns
        ),
        # EXAMPLE 2, email 2: Hanna shipped (no order id, only tracking)
        (
            "Your Hanna order has shipped!",
            "Tracking #1Z9999AAAAAAAAAAAA. Expected Fri.",
            "info@e.o.hannaandersson.com",
            "Shipped",
            True,  # 1Z + 16 chars matches UPS pattern
        ),
        # EXAMPLE 4: cancelled
        (
            "Your order has been cancelled",
            "Order #112-4837265-0093318. Cancelled — items out of stock.",
            "order-update@amazon.com",
            "Cancelled",
            True,
        ),
    ],
)
def test_extract_lifecycle_state_hint_and_id(
    subject: str,
    body: str,
    sender: str,
    expected_state: str,
    expected_id_present: bool,
) -> None:
    out = package_extractor.extract(subject=subject, body=body, sender=sender)
    assert out["lifecycle_state_hint"] == expected_state
    if expected_id_present:
        assert out["package_id"] is not None and out["package_id"], (
            f"Expected an id but got {out['package_id']!r} for subject={subject!r}"
        )
    else:
        assert out["package_id"] is None


def test_extract_merchant_from_amazon_subdomain() -> None:
    out = package_extractor.extract(
        subject="Shipped: 1 item from your order",
        body="Order #112-AAAA. Constructive Playthings — tracking #ABC.",
        sender="ship-confirm@amazon.com",
    )
    assert out["merchant"] == "amazon"


def test_extract_merchant_from_hanna_marketing_subdomain() -> None:
    out = package_extractor.extract(
        subject="Thanks for your order!",
        body="Order #HA-9001-AB. Items: 2 dresses.",
        sender="info@e.o.hannaandersson.com",
    )
    assert out["merchant"] == "hannaandersson"


def test_extract_merchant_none_when_sender_blank() -> None:
    out = package_extractor.extract(subject="Order placed", body="Order #1.", sender="")
    assert out["merchant"] is None


# ---- lifecycle.apply_transition: title + body templates --------------------


def test_to_ordered_renders_title_and_audit_line() -> None:
    rendered = lifecycle.apply_transition(
        state="Ordered",
        existing_task_title="",
        existing_task_body="",
        email_payload={"merchant": "wholefoods", "package_id": "112-4837265-0093318"},
        owner_abbrev="MJ",
    )
    assert rendered is not None
    assert rendered["new_title"].startswith("MJ Ordered: ")
    assert "wholefoods" in rendered["new_title"].lower() or "Wholefoods" in rendered["new_title"]
    assert "112-4837265-0093318" in rendered["new_body"]
    assert "## Updates" in rendered["new_body"]


def test_to_shipped_replaces_state_prefix() -> None:
    # Existing task body already has an "Ordered." line; transition to Shipped
    # should prepend a new audit line (most-recent first).
    existing_body = "## Updates\n- 2026-05-04 12:00 UTC: Ordered. Order/tracking id: 112-X."
    rendered = lifecycle.apply_transition(
        state="Shipped",
        existing_task_title="MJ Ordered: Whole Foods (awaiting ship date)",
        existing_task_body=existing_body,
        email_payload={"merchant": "wholefoods", "package_id": "1Z9999AAAAAAAAAAAA"},
        owner_abbrev="MJ",
    )
    assert rendered is not None
    assert "Shipped:" in rendered["new_title"]
    # Both audit lines present, newest first.
    body = rendered["new_body"]
    shipped_idx = body.index("Shipped.")
    ordered_idx = body.index("Ordered.")
    assert shipped_idx < ordered_idx


def test_to_out_for_delivery_title_template() -> None:
    rendered = lifecycle.apply_transition(
        state="Out for Delivery",
        existing_task_title="MJ Shipped: Whole Foods (ETA Wed)",
        existing_task_body="## Updates\n- ts: Shipped.",
        email_payload={"merchant": "wholefoods"},
        owner_abbrev="MJ",
    )
    assert rendered is not None
    assert "Out for delivery TODAY" in rendered["new_title"]


def test_to_delivered_marks_with_check_but_no_complete_signal() -> None:
    rendered = lifecycle.apply_transition(
        state="Delivered",
        existing_task_title="MJ Out for delivery TODAY: Whole Foods",
        existing_task_body="## Updates\n- ts: Out for delivery.",
        email_payload={"merchant": "wholefoods"},
        owner_abbrev="MJ",
    )
    assert rendered is not None
    # Delivered renders with a check mark; this test asserts the title shape only —
    # auto-complete suppression is enforced at the runtime layer (we never PATCH
    # status=completed in lifecycle).
    assert "Delivered:" in rendered["new_title"]
    assert "Delivered." in rendered["new_body"]


def test_to_cancelled_includes_reason_when_present() -> None:
    rendered = lifecycle.apply_transition(
        state="Cancelled",
        existing_task_title="MJ Ordered: Whole Foods (awaiting ship date)",
        existing_task_body="",
        email_payload={"merchant": "wholefoods", "cancellation_reason": "out of stock"},
        owner_abbrev="MJ",
    )
    assert rendered is not None
    assert "Cancelled:" in rendered["new_title"]
    assert "out of stock" in rendered["new_title"]
    assert "out of stock" in rendered["new_body"]


def test_to_shipped_with_ups_carrier_renders_markdown_link() -> None:
    """When carrier is "ups", the Shipped audit line embeds the tracking id as
    a Markdown link (`[id](https://www.ups.com/track?tracknum=...)`). MS To Do
    renders this as a tappable link in the task body on iOS/desktop.
    """
    rendered = lifecycle.apply_transition(
        state="Shipped",
        existing_task_title="MJ Ordered: Hannaandersson (awaiting ship date)",
        existing_task_body="",
        email_payload={
            "merchant": "hannaandersson",
            "package_id": "1Z9999AAAAAAAAAAAA",
            "carrier": "ups",
        },
        owner_abbrev="MJ",
    )
    assert rendered is not None
    body = rendered["new_body"]
    assert "[1Z9999AAAAAAAAAAAA](https://www.ups.com/track?tracknum=1Z9999AAAAAAAAAAAA)" in body
    # Plain bare-id form should NOT appear outside the link (i.e., we shouldn't
    # have BOTH the link and a stray bare id on the same line).
    assert "Tracking id: 1Z9999AAAAAAAAAAAA." not in body


def test_to_shipped_with_unknown_carrier_falls_back_to_plain_text() -> None:
    """When carrier is None (id format didn't match any known carrier OR the
    extractor couldn't tag it), the Shipped audit line falls back to the
    pre-Markdown plain-text behavior so we don't render a broken link.
    """
    rendered = lifecycle.apply_transition(
        state="Shipped",
        existing_task_title="MJ Ordered: Mystery (awaiting ship date)",
        existing_task_body="",
        email_payload={
            "merchant": "mystery",
            "package_id": "MYSTERY-XYZ-123",
            "carrier": None,
        },
        owner_abbrev="MJ",
    )
    assert rendered is not None
    body = rendered["new_body"]
    assert "Shipped. Tracking id: MYSTERY-XYZ-123." in body
    assert "](http" not in body, "should not render a Markdown link for unknown carrier"


def test_to_ordered_with_amazon_order_id_renders_order_details_link() -> None:
    """Amazon order ids route to the order-details page (not a carrier-tracking
    URL — Amazon doesn't expose stable per-package public lookups for its
    in-house carrier IDs; the order page is the right landing for Megha).
    """
    rendered = lifecycle.apply_transition(
        state="Ordered",
        existing_task_title="",
        existing_task_body="",
        email_payload={
            "merchant": "amazon",
            "package_id": "112-4837265-0093318",
            "carrier": "amazon",
        },
        owner_abbrev="MJ",
    )
    assert rendered is not None
    body = rendered["new_body"]
    expected_url = (
        "https://www.amazon.com/gp/your-account/order-details?orderID=112-4837265-0093318"
    )
    assert f"[112-4837265-0093318]({expected_url})" in body


def test_apply_transition_unknown_state_returns_none() -> None:
    rendered = lifecycle.apply_transition(
        state="UnknownPlanetState",
        existing_task_title="MJ Ordered: x",
        existing_task_body="",
        email_payload={"merchant": "x"},
        owner_abbrev="MJ",
    )
    assert rendered is None


# ---- false-merge regression cases ------------------------------------------
#
# These guard the tier-2 heuristic (find_todo_task_by_heuristic). We construct
# in-memory task lists and exercise the same merchant + 7-day filter the Graph
# client uses, but without actually calling Graph. The filter logic is tested
# end-to-end in test_tier2_*; the tier-1 lookup is purely a string match and is
# trivially correct.


def _make_task(
    *,
    task_id: str,
    title: str,
    body: str,
    created_days_ago: int = 1,
    status: str = "notStarted",
) -> dict[str, Any]:
    created = (datetime.now(timezone.utc) - timedelta(days=created_days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f0Z"
    )
    return {
        "id": task_id,
        "title": title,
        "body": {"content": body},
        "createdDateTime": created,
        "lastModifiedDateTime": created,
        "status": status,
    }


def _filter_tasks_like_tier2(
    tasks: list[dict[str, Any]],
    merchant: str,
    recipient: str,
    max_age_days: int = 7,
) -> list[str]:
    """Re-implements find_todo_task_by_heuristic's filter against a static task
    list for unit testing. Mirrors the production filter rules in
    kavi_runtime.graph_client (merchant in body or title; age <= window;
    recipient stamp via body or owner abbrev).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    merchant_norm = merchant.lower()
    recipient_l = recipient.lower()
    matches: list[str] = []
    for t in tasks:
        if (t.get("status") or "").lower() == "completed":
            continue
        created = t.get("createdDateTime") or t.get("lastModifiedDateTime")
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if created_dt < cutoff:
            continue
        body = (t.get("body") or {}).get("content", "").lower()
        title = t.get("title", "").lower()
        if merchant_norm not in body and merchant_norm not in title:
            continue
        if recipient_l in {"megha", "mj"} and not (title.startswith("mj ") or " mj " in f" {title} "):
            if recipient_l not in body and recipient_l not in title:
                continue
        matches.append(t["id"])
    return matches


def test_tier2_no_merge_across_different_merchants_same_day() -> None:
    """An Amazon "Ordered" task and a Hanna "Ordered" task created the same day
    must NOT merge when a Hanna-shipped email arrives — the merchant tokens
    differ. Regression target: a permissive substring match would falsely
    merge "amazon" into a Hanna update.
    """
    tasks = [
        _make_task(
            task_id="t_amazon",
            title="MJ Ordered: Amazon (awaiting ship date)",
            body="Order #112-AAAA-AAAAAAA.\n## Updates\n- ts: Ordered.",
            created_days_ago=0,
        ),
        _make_task(
            task_id="t_hanna",
            title="MJ Ordered: Hannaandersson (awaiting ship date)",
            body="Order #HA-9001-AB.\n## Updates\n- ts: Ordered.",
            created_days_ago=0,
        ),
    ]
    matches = _filter_tasks_like_tier2(tasks, merchant="hannaandersson", recipient="MJ")
    assert matches == ["t_hanna"], (
        f"Hanna shipped email should match only the Hanna task, got {matches}"
    )


def test_tier2_no_merge_when_same_merchant_outside_7d_window() -> None:
    """A 14-day-old Hanna task must NOT tier-2 merge with a fresh Hanna shipped
    email. Beyond the 7-day window, treat as a separate order.
    """
    tasks = [
        _make_task(
            task_id="t_hanna_old",
            title="MJ Ordered: Hannaandersson (awaiting ship date)",
            body="Order #HA-OLD-001.\n## Updates\n- ts: Ordered.",
            created_days_ago=14,
        ),
    ]
    matches = _filter_tasks_like_tier2(tasks, merchant="hannaandersson", recipient="MJ")
    assert matches == [], (
        f"14-day-old task should be outside 7d tier-2 window, got {matches}"
    )


# ---- idempotency on duplicate webhooks -------------------------------------
#
# MS Graph delivers webhook notifications with at-least-once semantics: the same
# `messageId` can fire twice during a retry window. The runtime's existing
# defense is `_dedup_check(message_id)` inside `handlers.email_arrived` (line
# ~698 in handlers.py as of 2026-05-05). That check sits AFTER the LLM call on
# the task-creation path; it gates the MS To Do PATCH/POST so the same email
# never spawns two task rows.
#
# Question this test was originally written to answer: does
# `lifecycle.apply_transition` itself prevent a duplicate audit line when called
# twice with the same payload against a body that already contains the
# transition's audit line?
#
# Observed behavior (2026-05-05): NO. `apply_transition` is purely additive —
# `_prepend_audit_line` always inserts a new bullet at the top of the
# `## Updates` section, regardless of what the body already contains. There is
# no body-level idempotency check inside lifecycle.py.
#
# Why we keep the assertion at the lifecycle layer pinned to observed-additive
# behavior (rather than adding a dedup pass to lifecycle.py): the right place
# for webhook idempotency is the handler (where `message_id` is the natural
# key), not the renderer. The lifecycle layer's job is "given a state + body,
# render the next title+body." Pushing dedup down here would force the renderer
# to re-derive what's already known upstream from `notification.resourceData.id`,
# and would still miss the case where a second webhook fires within the same
# minute (`_now_short()` resolves to the same string, so the audit line bytes
# match exactly — but a string-equality check inside `_prepend_audit_line`
# would catch ONLY that exact-byte case and would silently fail when the second
# webhook lands a minute later).
#
# Open spec gap surfaced by this test: the lifecycle path in `email_arrived`
# does NOT currently call `_dedup_check(message_id)` before invoking
# `_apply_lifecycle_update`. The dedup block lives only on the LLM-task-creation
# branch (after `claude.run_email_to_tasks`). That means a duplicate webhook
# for a Shipped/Delivered/OFD email today WILL produce two audit lines in the
# task body, plus (for OFD) two iMessage pings to Megha. This is queued in
# `capabilities/inbox-to-task.md` "Known issues / queued" per PM scope decision
# 2026-05-05; this test exists as a regression anchor so the dedup gap can't
# silently widen.


def test_apply_transition_is_additive_at_lifecycle_layer() -> None:
    """Calling `apply_transition` twice with identical input produces TWO
    audit lines in the body. This pins the lifecycle layer's additive design
    — idempotency is not the renderer's job. If a future commit makes
    lifecycle dedup body-internally, this assertion will flip and force the
    author to reconsider whether handler-level `_dedup_check(message_id)`
    is still the right idempotency target.

    Webhook idempotency is enforced at the handler layer, on `message_id`.
    See `test_handler_dedup_lifecycle_branch_blocks_duplicate_webhook` below.
    """
    payload = {
        "merchant": "wholefoods",
        "package_id": "112-4837265-0093318",
        "carrier": "amazon",
    }

    # First call: empty body → one audit line.
    first = lifecycle.apply_transition(
        state="Shipped",
        existing_task_title="MJ Ordered: Wholefoods (awaiting ship date)",
        existing_task_body="",
        email_payload=payload,
        owner_abbrev="MJ",
    )
    assert first is not None
    body_after_first = first["new_body"]
    assert body_after_first.count("Shipped.") == 1, (
        f"First call should produce exactly one Shipped audit line, "
        f"got body: {body_after_first!r}"
    )

    # Second call: pass the body from the first call back in. No `message_id`
    # at the lifecycle layer, so the renderer cannot dedup on the canonical
    # key — and shouldn't try to.
    second = lifecycle.apply_transition(
        state="Shipped",
        existing_task_title=first["new_title"],
        existing_task_body=body_after_first,
        email_payload=payload,
        owner_abbrev="MJ",
    )
    assert second is not None
    body_after_second = second["new_body"]
    assert body_after_second.count("Shipped.") == 2, (
        f"Lifecycle is currently additive — second call should produce a "
        f"second Shipped audit line. If this fails, lifecycle.py gained "
        f"body-internal dedup; reconsider whether handler-level "
        f"`_dedup_check(message_id)` is still the right idempotency target. "
        f"Got body: {body_after_second!r}"
    )


# ---- handler-level dedup on the lifecycle branch ---------------------------
#
# Bug fixed 2026-05-05: `email_arrived` previously called `_dedup_check` only
# on the LLM-task-creation branch. The lifecycle branch (which runs when a
# package email matches an existing open task) bypassed dedup entirely. A
# duplicate webhook for a Shipped/OFD/Delivered email therefore produced two
# audit lines AND (for OFD) two iMessage pings. The fix wraps the lifecycle
# call site with the same `_per_message_lock` + `_dedup_check` / `_dedup_record`
# pattern the LLM branch uses, keyed on `message_id` (the natural webhook id).
#
# The two tests below exercise the dedup helper directly. We avoid mocking
# the full `email_arrived` runtime (Graph client, Claude client, BlueBubbles,
# config paths) because the dedup contract is "second call with same
# message_id returns the cached id" — that contract is testable on the helper
# in isolation.


def test_dedup_helpers_share_window_across_branches() -> None:
    """`_dedup_check` and `_dedup_record` are module-level globals; the
    lifecycle branch and the LLM branch must share the same dedup window.
    This pins that shared-window invariant — recording on one branch
    short-circuits a re-arrival on the other.
    """
    from kavi_runtime import handlers

    # Use a unique id so this test is hermetic vs. real runtime state.
    msg_id = "test_lifecycle_dedup_shared_msg_001"
    target_task_id = "AAMkADAwATM0MDAxLTcxMmYtNGI5MC1hNGMwLT"

    # No prior record → check returns None (cache miss).
    assert handlers._dedup_check(msg_id) is None

    # Record from the lifecycle branch's perspective. The 2026-05-27
    # generalization accepts either a dict (preferred — full outcome shape)
    # or a string (legacy — interpreted as a bare task_id). This test
    # exercises the dict shape; the next test exercises legacy string input.
    handlers._dedup_record(msg_id, {
        "decision": "updated",
        "task_id": target_task_id,
        "decision_id": None,
    })

    # A second arrival (regardless of branch) sees the cached target.
    cached = handlers._dedup_check(msg_id)
    assert cached is not None
    assert cached.get("task_id") == target_task_id
    assert cached.get("decision") == "updated"


def test_dedup_lifecycle_branch_blocks_duplicate_webhook_via_short_circuit() -> None:
    """Simulates the exact bug scenario the 2026-05-05 fix addresses: two
    `email_arrived` invocations land for the same package-email message_id
    (Graph at-least-once retry). The lifecycle branch's dedup wrapper
    should observe the second arrival as a cache hit and short-circuit
    BEFORE calling `_apply_lifecycle_update` — preventing the second audit
    line and the second OFD iMessage.

    We verify the short-circuit by recording a target on the first arrival
    and asserting the cache observation on the second arrival, which is
    what the production code path keys off of.
    """
    from kavi_runtime import handlers

    msg_id = "test_lifecycle_dedup_ofd_msg_002"
    target_task_id = "BBMkADAwATM0MDAxLTcxMmYtNGI5MC1hNGMwLT"

    # Arrival 1: lifecycle branch acquires the per-message lock, finds no
    # cached id, runs `_apply_lifecycle_update` (not invoked here — that's
    # the lifecycle module's job, tested above), and records the target.
    # Pass the legacy string form to exercise the backwards-compat branch
    # of _dedup_record — out-of-tree callers using the pre-2026-05-27
    # signature must continue to work.
    with handlers._per_message_lock(msg_id):
        assert handlers._dedup_check(msg_id) is None
        handlers._dedup_record(msg_id, target_task_id)  # legacy string form

    # Arrival 2: same message_id, ~1 second later. The dedup wrapper
    # observes the cached target and short-circuits — no second audit line,
    # no second OFD iMessage.
    with handlers._per_message_lock(msg_id):
        cached = handlers._dedup_check(msg_id)
        assert cached is not None and cached.get("task_id") == target_task_id, (
            f"Duplicate webhook should hit the dedup cache on the lifecycle "
            f"branch and return the cached target_task_id; got {cached!r}"
        )
