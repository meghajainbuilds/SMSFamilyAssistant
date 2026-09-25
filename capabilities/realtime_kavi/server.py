"""FastAPI webhook receiver. Two endpoints: MS Graph notifications and BlueBubbles inbound."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from kavi_runtime.bluebubbles_client import BlueBubblesClient
from kavi_runtime.runtime.guardrails import (
    enqueue_deferred,
    log_trip,
    record_webhook_arrival,
    should_send_flood_alert,
    webhook_flood_active,
)
from kavi_runtime.graph_client import (
    discover_household_accounts,
    subscription_state_path_for,
)
from kavi_runtime.handlers import _get_clients, email_arrived, imessage_received
from kavi_runtime import handler_alerts
from capabilities.realtime_kavi.runtime_health import is_funnel_reachable
from capabilities.realtime_kavi.state_invariants import (
    check_startup_invariants,
    log_startup_invariants,
)
from kavi_runtime.structured_log import log_event

# Wrap the top-level handlers with the alerting decorators (added 2026-05-06
# in response to the silent-degradation gap: process up + Healthchecks
# heartbeat firing, but iMessage handler raises three times in a row on an
# Anthropic 529 wave and Megha gets no signal). The wrappers add Signals 1,
# 2, 3 and re-raise so the existing logger.exception path below is unchanged.
imessage_received = handler_alerts.wrap_imessage_handler(imessage_received)
email_arrived = handler_alerts.wrap_email_handler(email_arrived)

logger = logging.getLogger(__name__)


def _load_subscription_index(graph_accounts: list[str]) -> dict[str, dict[str, str]]:
    """Walk every per-account subscription state file and return a lookup
    map: subscription_id -> {"account": <email>, "client_state": <token>}.

    The legacy single-account fallback was removed when the startup
    migration landed (graph_client._migrate_legacy_subscription_state_forward).
    Any pre-existing legacy file is forward-migrated at GraphClient
    construction, so this walk is now the only code path that reads
    subscription state.
    """
    index: dict[str, dict[str, str]] = {}
    for account in graph_accounts:
        path = subscription_state_path_for(account)
        if not path.exists():
            continue
        try:
            state = json.loads(path.read_text())
        except Exception as e:
            logger.warning("server: failed to read subscription state for %s: %s", account, e)
            continue
        sid = state.get("subscription_id")
        cs = state.get("client_state")
        if sid:
            index[sid] = {"account": account, "client_state": cs or ""}
    return index


def build_app(config: dict, *, enforce_invariants: bool = False) -> FastAPI:
    app = FastAPI(title="kavi-runtime", version="0.2.0")
    # Only Graph notifications and /health answer on the public Funnel
    # hostname; everything else is local or tailnet only (public_gate.py).
    from capabilities.realtime_kavi.public_gate import PublicGate
    app.add_middleware(PublicGate)
    inbound_path = config["bluebubbles"]["inbound_webhook_path"]

    # STAGING MODE (added 2026-06-10, capability-build pipeline). When
    # config.staging_mode is true this instance is the LLM-replay sandbox
    # (port 8081): synthetic compose/verify routes, /health and /status
    # work normally (the LLM client is real — matrix replays need real
    # composer calls), but webhook ingestion returns 503 "staging" so no
    # Graph notification or BlueBubbles inbound is ever processed here.
    # Outbound is hard-blocked in runtime/send_imessage.py and scheduler
    # jobs never register (capabilities/realtime_kavi/scheduler.py).
    staging_mode = bool(config.get("staging_mode"))

    def _staging_503() -> Response:
        return Response(
            content=json.dumps({
                "error": "staging",
                "detail": (
                    "webhook processing is disabled on the staging instance; "
                    "this runtime serves synthetic replay routes only"
                ),
            }),
            status_code=503,
            media_type="application/json",
        )

    # Startup GC for pending_facts.jsonl (added 2026-05-27). Prunes any
    # rows whose `expires_at` is in the past so a cold-boot doesn't replay
    # week-old "facts to confirm" into the next periodic_summary. Atomic
    # rewrite with a uniquely-named tmp file inside the helper. Failure-
    # safe — a prune error must not block process startup.
    try:
        from kavi_runtime.runtime import durable_facts as _df_startup
        _pruned = _df_startup.prune_expired_pending_facts(config=config)
        if _pruned:
            logger.info(
                "server.build_app: pruned %d expired pending fact(s) at startup",
                _pruned,
            )
    except Exception as _startup_prune_err:
        logger.warning(
            "server.build_app: startup pending-facts prune failed (continuing): %s",
            _startup_prune_err,
        )

    # Startup self-check. Runs once per process. When enforced (production
    # path), CRITICAL violations flip /health to 503 so external monitoring
    # picks up the degradation; tests default to skip. Staging skips it too:
    # the invariants describe production state files and Graph accounts,
    # neither of which exists on the replay sandbox, and a degraded /health
    # there would fail deploy_staging.sh for reasons that have nothing to do
    # with what staging is for.
    if enforce_invariants and not staging_mode:
        try:
            accounts = discover_household_accounts(config)
            startup_violations = check_startup_invariants(accounts)
        except Exception as exc:
            logger.exception("state_invariants check failed; continuing degraded: %s", exc)
            startup_violations = []
        log_startup_invariants(startup_violations)
        has_critical_violations = any(
            v.severity == "CRITICAL" for v in startup_violations
        )
    else:
        startup_violations = []
        has_critical_violations = False

    @app.get("/health")
    def health() -> Response:
        violations: list[dict] = [
            {"severity": v.severity, "account": v.account,
             "message": v.message, "fix_command": v.fix_command}
            for v in startup_violations if v.severity == "CRITICAL"
        ]
        # Funnel reachability is dynamic (refreshed every 15 min by the
        # scheduler) — surface it on every /health poll so external
        # uptime monitors see the current state, not the boot-time snapshot.
        if not is_funnel_reachable():
            violations.append({
                "severity": "CRITICAL",
                "account": None,
                "message": (
                    "Tailscale Funnel public URL is not reachable from the "
                    "runtime. Microsoft Graph webhook deliveries are likely "
                    "returning 502 BadGateway."
                ),
                "fix_command": (
                    "verify `tailscale serve status` forward target matches "
                    "server.host:port the runtime is listening on"
                ),
            })
        if violations:
            return Response(
                content=json.dumps({"status": "degraded", "violations": violations}),
                status_code=503,
                media_type="application/json",
            )
        return Response(
            content='{"status":"ok"}',
            media_type="application/json",
        )

    @app.get("/status")
    def status_page() -> Response:
        """Pull-based runtime status (added 2026-05-26). Replaces the
        60-min "no activity" iMessage alert that was removed the same day.

        Single-page HTML, eight fields, plain language. Megha bookmarks
        this on her phone and opens it on demand. Cache-Control: no-store
        so a manual refresh always returns the current snapshot.

        No auth: the Tailscale Funnel URL is non-guessable and the data
        on the page is operational metadata (timestamps + spend totals),
        not user content. Same posture as the other status endpoints on
        this server.
        """
        from kavi_runtime.runtime_status import render_status_html
        try:
            html = render_status_html(config)
        except Exception as e:
            logger.exception("status_page render failed")
            html = (
                "<!DOCTYPE html><html><body><pre>"
                "Status page failed to render. See runtime err log.\n"
                f"Error: {e}"
                "</pre></body></html>"
            )
        return Response(
            content=html,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/runtime-metrics/cost")
    def runtime_metrics_cost() -> Response:
        """Serve the same cost dashboard the script writes to disk.

        Why: Megha wants the 7-day Anthropic spend visible from her
        laptop without SSH-ing into Kavi's Mac. She visits
        http://<kavi-tailnet-ip>:8080/runtime-metrics/cost over Tailscale
        and gets fresh HTML.

        Caching: regenerate per request. The 7-day scan reads a few MB
        of JSONL in well under 200ms — adding TTL caching saves nothing
        observable and adds a stale-data class of confusion. If request
        volume grows, swap in functools.lru_cache + TTL.
        """
        from kavi_runtime.cost_dashboard import render_dashboard_html
        try:
            html = render_dashboard_html(days=7)
        except Exception as e:
            logger.exception("runtime_metrics_cost render failed")
            return Response(
                content=f"<h1>Cost dashboard render failed</h1><pre>{e}</pre>",
                media_type="text/html",
                status_code=500,
            )
        return Response(content=html, media_type="text/html")

    @app.get("/evals/inbox-to-task/recent")
    def evals_inbox_recent(date: str | None = None, limit: int = 50) -> Response:
        """Eval surface for inbox-to-task. Returns recent decision rows from
        eval-inbox-judgments.jsonl, optionally filtered to one date.

        Query params:
        - date: YYYY-MM-DD (default: today in UTC). Filters to rows where
          ts starts with this prefix.
        - limit: max rows to return (default 50).

        Read by the weekly trace builder on Megha's Mac via Tailscale (the
        chat-based daily slash command surface retired 2026-05-27).
        No auth — consistent with other endpoints; tailnet is the perimeter.
        """
        from datetime import datetime, timezone
        path = Path(config["paths"]["eval_inbox_judgments_jsonl"])
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts", "")
                    if ts.startswith(target_date):
                        rows.append(obj)
        rows = rows[-limit:] if limit and len(rows) > limit else rows

        payload = {
            "date": target_date,
            "count": len(rows),
            "rows": rows,
        }
        return Response(
            content=json.dumps(payload),
            media_type="application/json",
        )

    @app.get("/evals/kavi-persona/inbound/recent")
    def evals_persona_inbound_recent(date: str | None = None, limit: int = 200) -> Response:
        """Eval surface for Kavi inbound iMessages. Returns rows from
        eval-persona-inbound.jsonl, optionally filtered to one date.

        Joined to outbound rows by inbound_id == outbound.triggered_by. Read by
        the weekly trace builder on Megha's Mac via Tailscale (the chat-based
        daily slash command surface retired 2026-05-27).
        """
        from datetime import datetime, timezone
        path = Path(config["paths"]["eval_persona_inbound_jsonl"])
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts", "")
                    if ts.startswith(target_date):
                        rows.append(obj)
        rows = rows[-limit:] if limit and len(rows) > limit else rows

        payload = {
            "date": target_date,
            "count": len(rows),
            "rows": rows,
        }
        return Response(
            content=json.dumps(payload),
            media_type="application/json",
        )

    @app.get("/evals/kavi-coordinates/recent")
    def evals_coordinates_recent(date: str | None = None, limit: int = 50) -> Response:
        """Eval surface for the Kavi coordinates capability. Returns rows from
        eval-coordinates-judgments.jsonl, optionally filtered to one date.

        Mirrors the /evals/inbox-to-task/recent and /evals/kavi-persona/recent
        endpoint shapes. Read by the /eval-coordinates slash command on Megha's
        Mac via Tailscale.
        """
        from datetime import datetime, timezone
        path_raw = config["paths"].get("eval_coordinates_judgments_jsonl")
        path = Path(path_raw) if path_raw else Path(
            "/Users/kavi/HomeOS/evals/kavi-coordinates/eval-coordinates-judgments.jsonl"
        )
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts", "")
                    if ts.startswith(target_date):
                        rows.append(obj)
        rows = rows[-limit:] if limit and len(rows) > limit else rows

        payload = {
            "date": target_date,
            "count": len(rows),
            "rows": rows,
        }
        return Response(
            content=json.dumps(payload),
            media_type="application/json",
        )

    @app.get("/evals/kavi-persona/recent")
    def evals_persona_recent(date: str | None = None, limit: int = 50) -> Response:
        """Eval surface for kavi-persona generative outputs. Returns recent outbound
        rows from eval-persona-outbound-judgments.jsonl, optionally filtered to one
        date.

        Read by the weekly trace builder on Megha's Mac via Tailscale (the
        chat-based daily slash command surface retired 2026-05-27).
        Mirrors the inbox-to-task endpoint shape.
        """
        from datetime import datetime, timezone
        path = Path(config["paths"]["eval_persona_outbound_judgments_jsonl"])
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts", "")
                    if ts.startswith(target_date):
                        rows.append(obj)
        rows = rows[-limit:] if limit and len(rows) > limit else rows

        payload = {
            "date": target_date,
            "count": len(rows),
            "rows": rows,
        }
        return Response(
            content=json.dumps(payload),
            media_type="application/json",
        )

    @app.post("/graph/notifications")
    async def graph_notifications(request: Request) -> Response:
        if staging_mode:
            # Staging holds no Graph subscriptions; even the validation-token
            # echo is refused so a misconfigured subscription can never bind
            # itself to the sandbox.
            return _staging_503()
        validation_token = request.query_params.get("validationToken")
        if validation_token:
            logger.info("graph subscription validation: token echoed")
            return Response(content=validation_token, media_type="text/plain")

        body: dict[str, Any] = await request.json()
        notifications = body.get("value", [])

        # Build the subscription -> account index once per request. Cheap (a
        # couple of small JSON reads) and avoids holding stale state across
        # subscription renewals or new-account onboarding.
        graph, _, _ = _get_clients(config)
        sub_index = _load_subscription_index(graph.accounts())

        for n in notifications:
            sub_id = n.get("subscriptionId")
            entry = sub_index.get(sub_id) if sub_id else None
            if entry is None:
                logger.warning(
                    "graph notification with unknown subscriptionId=%s; dropping",
                    (sub_id or "?")[:24],
                )
                continue
            account = entry["account"]
            expected_client_state = entry["client_state"]
            if expected_client_state and n.get("clientState") != expected_client_state:
                logger.warning(
                    "graph notification clientState mismatch for account=%s; dropping",
                    account,
                )
                continue

            # Step 12 guardrail: webhook flood. Track every inbound notification on
            # a rolling 60-min window. When over threshold, defer rather than dispatch
            # — the scheduler drains the queue at 1/minute. Cap on Sonnet calls is
            # already enforced upstream by the spend cap; this keeps surge bursts
            # from locking the event loop.
            count = record_webhook_arrival()
            threshold = config["guardrails"]["webhook_flood_per_hour"]
            if count > threshold:
                # Stash the source account on the deferred payload so the
                # scheduler-driven drain replays through the right account.
                enqueue_deferred({**n, "_source_account": account})
                logger.warning("webhook flood active (count=%d threshold=%d); deferred notification", count, threshold)
                if should_send_flood_alert():
                    log_trip(
                        Path(config["paths"]["runtime_events_jsonl"]),
                        guardrail="webhook_flood",
                        threshold=threshold,
                        actual=count,
                        action="throttled_to_1_per_min",
                    )
                    asyncio.create_task(_send_flood_alert(config, count, threshold))
                continue

            log_event("email", "email_arrived_start", config=config,
                      account=account, subscription_id=(sub_id or "")[:24])
            try:
                await asyncio.to_thread(email_arrived, n, config, account)
                log_event("email", "email_arrived_done", config=config, account=account)
            except Exception as e:
                log_event("email", "email_arrived_failed", config=config,
                          account=account, error=str(e)[:200])
                logger.exception(
                    "email_arrived failed for notification %s account=%s",
                    (sub_id or "?")[:12], account,
                )

        return Response(status_code=202)

    @app.post(inbound_path)
    async def bluebubbles_webhook(request: Request) -> Response:
        if staging_mode:
            return _staging_503()
        body: dict[str, Any] = await request.json()
        log_event("imessage", "imessage_received_start", config=config)
        try:
            await asyncio.to_thread(imessage_received, body, config)
            log_event("imessage", "imessage_received_done", config=config)
        except Exception as e:
            log_event("imessage", "imessage_received_failed", config=config,
                      error=str(e)[:200])
            logger.exception("imessage_received failed")
        return Response(status_code=202)

    @app.post("/synthetic/compose/inbox-to-task")
    async def synthetic_compose_inbox_to_task(request: Request) -> Response:
        """Synthetic replay surface for the inbox-to-task classifier +
        composer. Used by the Verifier sub-agent
        (`.claude/agents/verifier.md`) over Tailscale to re-run an
        Investigator's repro test after a fix.

        Body shape:
        ```json
        {
          "email": {
            "id": "...",
            "subject": "...",
            "from_name": "...",
            "from_address": "...",
            "to": ["..."],
            "received": "...",
            "body_text": "...",
            "source_account": "..."  // optional
          },
          "thread_messages": [],         // optional
          "applicable_corrections": []   // optional
        }
        ```

        Returns: `{"decision": str, "result": dict, "model": str,
        "input_payload": dict}`.

        Side effects on Kavi state: NONE. No MS To Do write, no eval JSONL
        write, no learned_facts mutation. Operational `structured_log` rows
        ARE emitted (audit trail).

        Auth: same posture as the other endpoints (Tailnet perimeter; no
        per-request auth).
        """
        from kavi_runtime.synthetic_compose import replay_email_classify

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        if not isinstance(body, dict):
            return Response(
                content=json.dumps({
                    "error": "invalid_body",
                    "detail": "request body must be a JSON object",
                }),
                status_code=400,
                media_type="application/json",
            )

        email = body.get("email")
        if not isinstance(email, dict):
            return Response(
                content=json.dumps({
                    "error": "missing_email",
                    "detail": "body must include an 'email' object",
                }),
                status_code=400,
                media_type="application/json",
            )

        thread_messages = body.get("thread_messages")
        applicable_corrections = body.get("applicable_corrections")

        try:
            result = await asyncio.to_thread(
                replay_email_classify,
                config,
                email,
                thread_messages=thread_messages,
                applicable_corrections=applicable_corrections,
            )
        except Exception as e:
            logger.exception("synthetic_compose_inbox_to_task: replay failed")
            return Response(
                content=json.dumps({"error": "replay_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    @app.post("/synthetic/compose/kavi-persona")
    async def synthetic_compose_kavi_persona(request: Request) -> Response:
        """Synthetic replay surface for the kavi-persona periodic_summary
        composer. Used by the Verifier sub-agent (`.claude/agents/verifier.md`)
        over Tailscale to re-run an Investigator's repro test after a fix.

        Body: state snapshot with `queued_tasks`, `pending_questions`,
        `pending_facts`, `is_rollup`, `time_of_day`. Additive 2026-06-10
        fields: `recipient`, `due_soon`, `close_suggestions`
        (suggest-to-close items, each {title, reason}), `theme`
        ({label, task_count} or the clusterer's raw
        {label, supporting_task_titles} shape). Additive 2026-06-11
        field: `stale_tasks` (stale-task nudge items, each
        {title, age_days}, oldest first).

        Returns: `{"output": str|None, "model": str, "input_payload": {...}}`.

        Side effects on Kavi state: NONE. The replay function in
        `kavi_runtime/synthetic_compose.py` calls only the composer LLM and
        returns; no iMessage send, no eval JSONL write, no state mutation.
        Operational `structured_log` rows ARE emitted (audit trail).

        Auth: same posture as the other endpoints on this server (Tailnet
        perimeter; no per-request auth).
        """
        from kavi_runtime.synthetic_compose import replay_periodic_summary

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        if not isinstance(body, dict):
            return Response(
                content=json.dumps({
                    "error": "invalid_body",
                    "detail": "request body must be a JSON object",
                }),
                status_code=400,
                media_type="application/json",
            )

        try:
            result = await asyncio.to_thread(replay_periodic_summary, config, body)
        except Exception as e:
            logger.exception("synthetic_compose_kavi_persona: replay failed")
            return Response(
                content=json.dumps({"error": "replay_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    @app.post("/synthetic/verify/kavi-persona")
    async def synthetic_verify_kavi_persona(request: Request) -> Response:
        """Selection-behavior deep verify for the kavi-persona periodic_summary
        composer. Added 2026-05-31 after the 2026-05-30 9 PM rollup proved
        that the prior output-shape Verifier missed selection-behavior bugs
        (re-surfaced a Q&A whose underlying task Megha had already marked
        done; re-anchored on a 9am Saturday event at 9 PM the same day).

        Body extends `/synthetic/compose/kavi-persona` with:
          - `closed_task_ids` (list[str]): task_ids the verifier treats as
            done. Gate asserts the composed output does NOT name any of
            their titles.
          - `past_event_titles` (list[str]): title strings the verifier
            treats as past events. Gate asserts the composed output does
            NOT name any of them.
          - `recipient` (str, default "megha"; added 2026-06-10): who the
            replayed digest is composed for ("megha" / "max"). The
            `owner_leak` gate asserts the output names no task whose
            title-prefix owner is the other person.
          - `due_soon` (list[{title, due_date, days_until}]; added
            2026-06-10): deadline-runway items. The `due_soon_dropped`
            gate asserts the composed morning digest references each one.
          - `close_suggestions` (list[{title, reason}]; added 2026-06-10):
            evening suggest-to-close items. The `close_claim` gate asserts
            the composed output uses suggestion language only — never
            completion-claim phrasing ("closed it", "marked done").
          - `theme` ({label, task_count} or {label,
            supporting_task_titles}; added 2026-06-10): morning
            top-of-mind theme. The `theme_unsupported` gate asserts the
            composed morning digest references the given theme's label
            keywords, and that no theme is invented when the payload
            carries none.
          - `stale_tasks` (list[{title, age_days}]; added 2026-06-11):
            stale-task nudge items (no-theme mornings only in live
            selection). The `stale_task_dropped` gate asserts the
            composed morning digest references each item's title
            keywords; no-op for rollups.

        Returns: `{"output": str|None, "verdict": "PASS"|"FAIL",
                  "failures": [{"gate": ..., "detail": ...}, ...],
                  "model": str, "input_payload": {...}}`.

        Side effects on Kavi state: NONE. Same guardrails as the raw
        compose endpoint above; we only call the LLM composer.

        Auth: same posture as the other endpoints on this server (Tailnet
        perimeter; no per-request auth).
        """
        from kavi_runtime.synthetic_compose import verify_periodic_summary_selection

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        if not isinstance(body, dict):
            return Response(
                content=json.dumps({
                    "error": "invalid_body",
                    "detail": "request body must be a JSON object",
                }),
                status_code=400,
                media_type="application/json",
            )

        try:
            result = await asyncio.to_thread(
                verify_periodic_summary_selection, config, body,
            )
        except Exception as e:
            logger.exception("synthetic_verify_kavi_persona: verify failed")
            return Response(
                content=json.dumps({"error": "verify_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    @app.post("/synthetic/verify/inbox-to-task")
    async def synthetic_verify_inbox_to_task(request: Request) -> Response:
        """Selection-behavior deep verify for the inbox-to-task classifier.
        Added 2026-06-02 (Phase 6 of the architectural refactor) to bring
        inbox-to-task to deep-verify parity with kavi-persona.

        Body extends `/synthetic/compose/inbox-to-task` with:
          - `expected_decision` (optional str): "task" | "skip" | etc.
            Gate asserts result['status'] matches.
          - `expected_owner` (optional str): "MJ" | "MM". Gate asserts
            result['task']['owner'] matches when decision is "task".
          - `expected_confidence` (optional str): "high" | "medium" |
            "low". Gate asserts result['confidence'] matches.

        Returns: `{"output": dict, "verdict": "PASS"|"FAIL",
                  "failures": [{"gate": ..., "detail": ...}, ...],
                  "model": str, "input_payload": dict}`.

        Side effects: NONE (no MS To Do write, no eval JSONL mutation, no
        learned_facts mutation). Operational structured_log rows ARE
        emitted.

        Auth: same posture as the other endpoints (Tailnet perimeter).
        """
        from kavi_runtime.synthetic_compose import verify_email_classify_selection

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        if not isinstance(body, dict):
            return Response(
                content=json.dumps({
                    "error": "invalid_body",
                    "detail": "request body must be a JSON object",
                }),
                status_code=400,
                media_type="application/json",
            )

        email = body.get("email")
        if not isinstance(email, dict):
            return Response(
                content=json.dumps({
                    "error": "missing_email",
                    "detail": "body must include an 'email' object",
                }),
                status_code=400,
                media_type="application/json",
            )

        try:
            result = await asyncio.to_thread(
                verify_email_classify_selection, config, body,
            )
        except Exception as e:
            logger.exception("synthetic_verify_inbox_to_task: verify failed")
            return Response(
                content=json.dumps({"error": "verify_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    @app.post("/synthetic/compose/kavi-reply")
    async def synthetic_compose_kavi_reply(request: Request) -> Response:
        """Raw replay mode for the kavi-persona inbound-reply path
        (intent-first dispatch rebuild, 2026-06-10). Runs the REAL intent
        parser over the synthetic context, the deterministic executors in
        DRY-RUN (no Graph calls, no sends, no state writes), then the REAL
        reply composer over the simulated execution results.

        Body (contract: evals/kavi-reply/matrix/ENDPOINT_CONTRACT.md §1,
        minus the two verifier-only expectation fields, which are ignored
        if present): `inbound_text`, `sender` ("megha"|"max"),
        `recent_outbound` [{kind, text}] (newest last), `recent_inbound`
        [string], `pending_questions`, `pending_facts`, `open_tasks`
        [{id, title}], optional `open_coordination_sessions`.

        Returns: `{output, parsed_intents, executed, model, input_payload}`.

        Side effects on Kavi state: NONE. Operational structured_log rows
        ARE emitted (audit trail). Auth: Tailnet perimeter.
        """
        from kavi_runtime.synthetic_compose import (
            replay_kavi_reply, validate_reply_payload,
        )

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        # Compose mode: expectation fields are tolerated (ignored), so an
        # Investigator can replay a matrix row's payload verbatim.
        body_for_validation = body
        if isinstance(body, dict):
            body_for_validation = {
                k: v for k, v in body.items()
                if k not in ("expected_intents", "forbidden_intents")
            }
        problem = validate_reply_payload(
            body_for_validation, expectations_required=False,
        )
        if problem is not None:
            return Response(
                content=json.dumps({"error": "invalid_body", "detail": problem}),
                status_code=400,
                media_type="application/json",
            )

        try:
            result = await asyncio.to_thread(
                replay_kavi_reply, config, body_for_validation,
            )
        except Exception as e:
            logger.exception("synthetic_compose_kavi_reply: replay failed")
            return Response(
                content=json.dumps({"error": "replay_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    @app.post("/synthetic/verify/kavi-reply")
    async def synthetic_verify_kavi_reply(request: Request) -> Response:
        """Gated verify mode for the kavi-persona inbound-reply path
        (`deep:reply_intent`, intent-first dispatch rebuild 2026-06-10).
        Same body as /synthetic/compose/kavi-reply PLUS the verifier-only
        `expected_intents` / `forbidden_intents` (each [{type,
        target_keyword}]). Unknown fields reject with 400 (contract §1).

        Gates (canonical logic: capabilities/kavi_persona/verify_reply.py):
        intent_dropped, wrong_direction_resolution, banned_template_reply,
        unresolved_context_claim, ungrounded_action_claim (negation-aware),
        length_cap (structural_checks.LENGTH_CAP_TARGET), prose_required.

        Returns: `{output, verdict, failures[], parsed_intents, executed,
        model, input_payload}`.

        Side effects on Kavi state: NONE. Auth: Tailnet perimeter.
        Frozen seed matrix: evals/kavi-reply/matrix/matrix-kavi-reply.jsonl.
        """
        from kavi_runtime.synthetic_compose import (
            verify_kavi_reply, validate_reply_payload,
        )

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        problem = validate_reply_payload(body, expectations_required=True)
        if problem is not None:
            return Response(
                content=json.dumps({"error": "invalid_body", "detail": problem}),
                status_code=400,
                media_type="application/json",
            )

        try:
            result = await asyncio.to_thread(verify_kavi_reply, config, body)
        except Exception as e:
            logger.exception("synthetic_verify_kavi_reply: verify failed")
            return Response(
                content=json.dumps({"error": "verify_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    @app.post("/synthetic/compose/kavi-coordinates")
    async def synthetic_compose_kavi_coordinates(request: Request) -> Response:
        """Synthetic replay surface for the kavi-coordinates addressee-message
        composer (the central user-visible LLM output of a coordination
        session). Added 2026-06-10 (Phase 0c closure for kavi-coordinates).
        Used by the Investigator sub-agent as the lower-level replay surface;
        Verifiers call /synthetic/verify/kavi-coordinates by default.

        Body: session payload with `inbound_text`, `requester_name`,
        `addressee_name`, `coordination_ask`, optional
        `attribution_judgment`.

        Returns: `{"output": str|None, "model": str, "input_payload": {...}}`.

        Side effects on Kavi state: NONE. No iMessage send, no session
        registry mutation, no durable-facts write, no eval JSONL write.
        Operational `structured_log` rows ARE emitted (audit trail).

        Auth: same posture as the other endpoints (Tailnet perimeter; no
        per-request auth).
        """
        from kavi_runtime.synthetic_compose import replay_coordination_addressee

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        if not isinstance(body, dict):
            return Response(
                content=json.dumps({
                    "error": "invalid_body",
                    "detail": "request body must be a JSON object",
                }),
                status_code=400,
                media_type="application/json",
            )

        try:
            result = await asyncio.to_thread(
                replay_coordination_addressee, config, body,
            )
        except Exception as e:
            logger.exception("synthetic_compose_kavi_coordinates: replay failed")
            return Response(
                content=json.dumps({"error": "replay_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    @app.post("/synthetic/verify/kavi-coordinates")
    async def synthetic_verify_kavi_coordinates(request: Request) -> Response:
        """Selection-behavior deep verify for the kavi-coordinates
        addressee-message composer. Added 2026-06-10 (Phase 0c closure)
        after the June 3 incident shipped a week of "coordinating with Max
        on something" digests — the vague-output class this endpoint's
        gates reject.

        Body: same session payload as /synthetic/compose/kavi-coordinates.

        Gates (see capabilities/coordination/verify.py):
          - shape: `length_cap`, `prose_required`
          - selection: `vague_addressee_message` (output must carry at
            least one content keyword from the injected coordination ask),
            `empty_content_outbound` (an empty-content session must not
            produce an outbound).

        Returns: `{"output": str|None, "verdict": "PASS"|"FAIL",
                  "failures": [{"gate": ..., "detail": ...}, ...],
                  "model": str, "input_payload": {...}}`.

        Side effects on Kavi state: NONE. Same guardrails as the raw
        compose endpoint above; we only call the LLM composer.

        Auth: same posture as the other endpoints (Tailnet perimeter; no
        per-request auth).
        """
        from kavi_runtime.synthetic_compose import verify_coordination_selection

        try:
            body = await request.json()
        except Exception as e:
            return Response(
                content=json.dumps({"error": "invalid_json", "detail": str(e)}),
                status_code=400,
                media_type="application/json",
            )

        if not isinstance(body, dict):
            return Response(
                content=json.dumps({
                    "error": "invalid_body",
                    "detail": "request body must be a JSON object",
                }),
                status_code=400,
                media_type="application/json",
            )

        try:
            result = await asyncio.to_thread(
                verify_coordination_selection, config, body,
            )
        except Exception as e:
            logger.exception("synthetic_verify_kavi_coordinates: verify failed")
            return Response(
                content=json.dumps({"error": "verify_failed", "detail": str(e)}),
                status_code=500,
                media_type="application/json",
            )

        return Response(
            content=json.dumps(result),
            media_type="application/json",
        )

    return app


async def _send_flood_alert(config: dict, count: int, threshold: int) -> None:
    """Background-fire an iMessage to Megha that we hit the webhook flood threshold.
    Debounced via guardrails.should_send_flood_alert (max once/hour).

    Routes through the canonical send wrapper so the recipient allowlist
    plus content scanner gates run on every flood alert. Defense in depth
    matters here even more than usual — a flood-alert leak would broadcast
    runtime-internal traffic shape.
    """
    text = (
        f"Heads up: I just got {count} inbound webhook events in the last hour "
        f"(threshold {threshold}). Throttling to 1/min for the next hour. "
        f"Probably an Outlook flood or a misconfig — taking a look."
    )
    try:
        from kavi_runtime.handlers import _send_imessage_with_fallback
        # AUDIT 2026-06-10: deterministic ops alert per the kavi-persona
        # Out of scope rule; counts grounded in the flood tracker.
        await asyncio.to_thread(
            lambda: _send_imessage_with_fallback(
                config, text, kind="alert_flood",
                provenance={"fallback_audit": "2026-06-10"},
            ),
        )
    except Exception:
        logger.exception("flood alert iMessage send failed")
