"""state_schemas.py — Pydantic models for the runtime's persistent state.

Why: 2026-05-06. Today's silent-failure incident corrupted imessage-state.json
in a way that wasn't caught at write time — only when the next reader
exploded on the malformed JSON tail. Pydantic validation at write time
catches programmer errors and shape regressions immediately, with a
traceback pointing at the call site, not at the unrelated next handler.

These schemas are intentionally permissive (extra fields allowed) so a new
runtime version can add fields without breaking older readers. Strictness
is reserved for the REQUIRED fields whose absence would silently break
the runtime — subscription_id, client_state, expiration_dt for
SubscriptionState; nothing required for ImessageState (it cold-starts).

Pairs with state_io.atomic_write_json's optional `schema` parameter.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ImessageState(BaseModel):
    """Shape of imessage-state.json. Captures pending Q&A questions,
    daily summary queue, last-send timestamps, and guardrail pause flags.

    All fields are optional with sensible defaults so a fresh runtime
    starts cleanly; a write that omits all of them is still a valid empty
    state. New fields can be added without breaking older runtime
    versions — `extra="allow"` permits unknown keys."""

    model_config = ConfigDict(extra="allow")

    questions: list[dict[str, Any]] = []
    summary_queue: list[dict[str, Any]] = []
    last_send_at: str | None = None
    last_summary_send_at: str | None = None
    auto_runs_paused: bool = False
    paused_since: str | None = None
    paused_reason: str | None = None
    # 2026-07-14: a time-boxed user_requested_quiet window's UTC-ISO end time
    # (None for spend/webhook/api pauses, which never auto-resume). The
    # scheduler auto-resumes once this elapses; liveness_alert_sent debounces
    # the stuck-pause alert to once per episode.
    paused_until: str | None = None
    liveness_alert_sent: bool = False
    paused_email_queue: list[dict[str, Any]] = []
    pending_alerts: list[dict[str, Any]] = []
    spend_cap_bypass_month: str | None = None
    # 2026-05-07: per-sender pending action clarifications. When the action
    # layer ships a clarifying reply with proposed matches (e.g., "two UW
    # tasks open: bill + $630 balance — mark both?"), the proposal is saved
    # here so the next inbound from the same sender ("yes, both") can resolve
    # it via the LLM and execute the matches. Without this, "Yes" reads as a
    # bare conversational message with no action context and Kavi replies
    # "Got it." without executing. Keyed by sender handle (phone or email).
    # Each entry expires 10 minutes after set_at.
    pending_action_clarifications: dict[str, dict[str, Any]] = {}
    # 2026-05-07 (Fix 2): per-sender summary-anchor state. When periodic_summary
    # mentions a specific task ("confirm your guest got the Zoom link"), the
    # task ID is saved here so a follow-up "Yes on Elders' Tea" can pick the
    # anchor as the primary action and offer siblings as a sweep, instead of
    # dumping a flat clarification of every Elders'-Tea-titled task. Keyed by
    # recipient handle. Each entry expires 30 minutes after set_at (slightly
    # longer than pending_action_clarifications because the user may not
    # reply for ~half an hour).
    last_summary_anchors: dict[str, dict[str, Any]] = {}


class SubscriptionState(BaseModel):
    """Shape of subscriptions/<account>.json. The 3 required fields gate
    every webhook routing decision; missing any of them silently breaks
    inbound mail. Optional fields are metadata for debugging."""

    model_config = ConfigDict(extra="allow")

    subscription_id: str
    client_state: str
    expiration_dt: str
    notification_url: str | None = None
    resource: str | None = None
    created_at: str | None = None
    account: str | None = None
