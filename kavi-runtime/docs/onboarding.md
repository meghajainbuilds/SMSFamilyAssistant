# Kavi runtime — onboarding

You're new to the project. This is a navigation map, not a tutorial. Read
the canonical specs (`capabilities/*.md`, `CLAUDE.md`, this file's
references) before changing behavior.

## What the runtime does

Kavi is the McMullen-Jain household's named Chief of Staff. The runtime is
an always-on Python service on a dedicated MacBook (`kavis-macbook-pro`,
Tailscale `100.64.0.10`) that watches Megha's and Max's Outlook inboxes
via Microsoft Graph webhooks, decides whether each inbound email is
actionable, writes tasks into a shared Microsoft To Do list, and sends
short iMessages to Megha and Max via BlueBubbles. It also responds to
inbound iMessages from Megha (corrections, Q&A replies, action verbs,
coordination requests, conversational chat) using Anthropic Sonnet.

## Entry points

Two webhook endpoints, both in `kavi_runtime/server.py`:

- **`server.py:graph_notifications`** (POST `/graph/notifications`). MS
  Graph posts here when a watched mailbox gets a new message. The handler
  validates the subscription clientState, applies the webhook flood
  guardrail, and dispatches to `handlers.email_arrived(notification, config, account)`.
- **`server.py:bluebubbles_webhook`** (POST `/imessage`). BlueBubbles
  posts here when an iMessage arrives at Kavi's Apple ID inbox. Dispatches
  to `handlers.imessage_received(payload, config)`.

Both run on `127.0.0.1:8080`, exposed via Tailscale Funnel at
`https://kavi-mac.your-tailnet.example`.

## Inbound + outbound paths

```
   Megha's / Max's Outlook                        Megha's iPhone
            |                                            |
            v (MS Graph webhook)                         v (iMessage to Kavi)
  +---------+---------+                       +----------+----------+
  | server.py         |                       | server.py           |
  | graph_notifications|                       | bluebubbles_webhook |
  +---------+---------+                       +----------+----------+
            |                                            |
            v                                            v
  handlers.email_arrived               handlers.imessage_received
            |                                            |
            v                                            v
  +-- inbox-to-task skill --+              correction / action-intent /
  |  (Anthropic Sonnet)     |              coordination / conversational
  +------------+------------+                            |
               |                                         |
               v                                         v
        outbound paths                            outbound paths
        ===============                           ===============
        - MS To Do task write   <-- graph_client.create_task_in_shared_list
        - iMessage notify       <-- handlers._send_imessage_with_fallback
        - durable-fact write    <-- durable_facts.record_fact
        - eval row              <-- runs.jsonl / eval-*.jsonl (state.append_run)
                                            (every outbound is gated by
                                             outbound_scanner before send)
```

## Read order to understand the system

1. `~/Documents/HomeOS/CLAUDE.md` — project navigation. Stack + role contract.
2. `~/Documents/HomeOS/capabilities/realtime-kavi.md` — runtime spec.
   Behavior, guardrails, error surfaces.
3. `~/Documents/HomeOS/capabilities/inbox-to-task.md` — the email-to-task
   judgment rules + Examples + Q&A learned patterns. Read this before
   changing anything `email_arrived` does.
4. `~/Documents/HomeOS/capabilities/kavi-persona.md` — the iMessage voice
   layer. Read before changing any composer.
5. `~/Documents/HomeOS/capabilities/kavi-coordinates.md` — the coordination
   capability (Megha asks Kavi to relay something to Max).
6. `~/Documents/HomeOS/household.md` — identity-only spec for who's in the
   household and how to reach them. The `outbound_scanner.HOUSEHOLD_HANDLES`
   constant must stay in sync with the iMessage handles table here.
7. `kavi_runtime/server.py` — webhook endpoints.
8. `kavi_runtime/handlers.py` — every event handler. Large (~2900 lines);
   see `audits/handlers_complexity_2026-05-06.md` for extraction candidates.
9. `kavi_runtime/scheduler.py` — every cron / interval job.
10. `kavi_runtime/claude_client.py` — every Anthropic API call. Each method
    is one call site; pricing audit at `audits/model_audit_2026-05-06.md`.
11. `kavi_runtime/graph_client.py` — every MS Graph call. Multi-account
    aware as of 2026-05-05.
12. `kavi_runtime/outbound_scanner.py` — deterministic security gates that
    sit on every outbound. Inherited by every Kavi-voiced surface.

## How to run tests

From the repo root on Megha's Mac:

```
cd kavi-runtime
.venv/bin/python -m pytest tests/ -x -q
```

155+ tests. New modules ship with their own `tests/test_<module>.py`. Tests
must run on the developer Mac without needing live MS Graph or Anthropic
credentials — fixtures only.

## How to deploy

The repo is rsync'd from Megha's Mac to Kavi's Mac. Per-file scp also
works for hot fixes:

```
scp kavi-runtime/kavi_runtime/<file>.py kavi@100.64.0.10:/Users/kavi/kavi-runtime/kavi_runtime/<file>.py
```

After deploy, restart the launchd service:

```
ssh kavi@100.64.0.10 "launchctl kickstart -k gui/$(ssh kavi@100.64.0.10 id -u)/com.megha.kavi"
```

Wait ~8 seconds, then verify:

```
curl https://kavi-mac.your-tailnet.example/health
ssh kavi@100.64.0.10 "tail -50 /tmp/kavi-runtime.err"
```

Per-account onboarding (first time only): see
`kavi_runtime/add_account.py` docstring.

## How to read logs

Three surfaces:

- **Free-text Python logger** — `tail -f /tmp/kavi-runtime.err` (default
  launchd stderr). Best for tracing one event end to end.
- **Structured JSON log** (added 2026-05-06) — `~/Library/Logs/kavi-runtime.json.log`.
  One JSON line per high-value event. Used by the daily error-budget rollup.
  Grep with `jq`: `jq 'select(.event=="email_arrived_failed")' kavi-runtime.json.log`.
- **Eval surfaces** — JSONL files under `~/HomeOS/evals/<capability>/`.
  Per-decision rows consumed by the shared HTML viewer at `evals/viewer.html`
  for weekly open coding (chat-based daily slash command surfaces retired
  2026-05-27); `/eval-coordinates` is the one remaining daily chat-based
  surface. Read these before changing anything that affects judgment or persona.

## Pointers for common tasks

- New capability spec: copy `Templates/HomeOS-capability.md`. Behavior +
  metrics + decision changelog inline. Don't fork.
- New module: drop in `kavi_runtime/`, add tests in `tests/`, wire into
  `scheduler.py` if scheduled, into `server.py` if HTTP-triggered, into
  `handlers.py` if event-driven from a webhook.
- Pricing model swap: see `audits/model_audit_2026-05-06.md` first; the
  per-call-type model routing is not yet wired (see "Recommended
  sequencing" in that audit).
- Eval failure investigation: read the eval surface for the relevant
  capability (under `~/HomeOS/evals/<capability>/`); pair with the
  free-text log for the same UTC timestamp; cross-reference
  `runs.jsonl`.

## Standing items in the runtime root

- `audits/` — point-in-time audit notes (model audit, complexity audit,
  household-handles drift, backup target options). Dated.
- `runtime_metrics/` — generated reports (cost dashboard HTML, daily
  error-budget markdown).
- `snapshots/` — nightly state-file copies, 14-day retention. Lives on
  Kavi's Mac at `/Users/kavi/kavi-runtime/snapshots/`.
- `scripts/` — one-off scripts (cost dashboard renderer, secrets
  migration, refusal-test runner, send-test).
- `skills/` — Anthropic skill prose (loaded into system prompts).
- `tests/` — pytest suite.
- `launchd/` — the `com.megha.kavi.plist` service definition.
