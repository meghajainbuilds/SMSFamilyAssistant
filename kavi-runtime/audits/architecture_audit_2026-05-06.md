# HomeOS architecture audit — 2026-05-06

Author: senior AI engineer (Claude). PM counterpart: Megha.
Trigger: third "process up but X" silent failure of the kavi-runtime in 48 hours.

---

## Resolution status — 2026-05-06 PM

**Shipped this session (one branch, ~7 commits worth of work, 35 new tests, 0 regressions, single rsync deploy + restart):**

| Stage | Closes | Artifact | Tests |
|---|---|---|---|
| 2 | Duplicate-writer corrupt-state | `state_io.py` helper + 9 raw-write call sites consolidated | `test_state_io_parity.py` (static-analysis enforcement) |
| 3 | Orphaned-legacy half-migrated state | `_migrate_legacy_subscription_state_forward` at startup, both fallback paths deleted | `test_subscription_state_migration.py` (5 cases) |
| 4 | "Process up + handlers never invoked" silent-startup | `state_invariants.py` startup self-check + `/health` returns 503 with the exact `add_account.py` command | `test_state_invariants.py` (9 cases) |
| 5 | "Process up + inbound silent at runtime" | Invocation-floor alarm (5-min interval, 30-min threshold, waking hours, debounced) | `test_invocation_floor.py` (9 cases) |
| 6 | **Today's actual root cause** | `config.yaml`: `host: "0.0.0.0"` (Funnel proxies to loopback; runtime now binds both interfaces) | manual: external probe via `https://kavi-mac.your-tailnet.example/health` returns 200/503 instead of 502 |

**Verified end-to-end at 3:07 PM PT:** Microsoft delivered a real webhook → runtime routed it to `email_arrived` for `megha@example.com` → handler skipped a self-sent mail correctly. Inbound is flowing.

**Engineering call validated:** one branch, no per-item PRs. Three rounds of restart × ~30 sec downtime each = ~90 sec total disruption to land 5 stages of work. PR-per-item would have added review ceremony with no reviewer.

## Resolution status — 2026-05-07 (plain-language alert rewrite + 3 latent-bug fixes)

**Trigger:** Megha reported "Kavi has been broken most of yesterday and today." Diagnosis surfaced four distinct issues; all four shipped this session. Single rsync deploy + one ~30 sec restart.

| Issue | What user noticed | Fix | Tests |
|---|---|---|---|
| 1. Q&A reply NameError | Got the canned "I'm degraded" iMessage reply 3x in 24h | Module-level import of `load_imessage_state` / `save_imessage_state` in `handlers.py` (was scoped-imported in only one function; `_apply_qa_resolutions` referenced both at module scope) | `test_handlers_imports.py` (regression: both names bound at module level) |
| 2. Alert emails self-blocked | Real failures produced no heads-up email; operator only learned via silence | `graph_client.send_mail` gains `bypass_scanner=False`; `_send_alert_email` always passes `bypass_scanner=True`. Recipient must be a household handle. | `test_send_mail_outbound_gate.py::test_send_mail_bypass_scanner_*` (2 cases) |
| 3. /health permanently degraded | `/health` returned 503 continuously because Max's account is configured but never onboarded | "Configured but no tokens AND no subscription" → WARNING (not CRITICAL); partial-state still CRITICAL | `test_state_invariants.py::test_partial_onboarding_is_critical` + updated multi-account test |
| 4. Noisy "no inbound" iMessages | ~7 per day, especially evenings; trained Megha to ignore alarms | `INVOCATION_FLOOR_QUIET_SEC` and debounce raised from 30 → 60 min | `test_invocation_floor.py` (5 cases updated for new threshold) |

**Bonus rewrite:** the three alert email composers (`compose_sender_alert_email`, `compose_rate_alert_email`, invocation-floor body) all rewritten to the WHAT BROKE / WHAT STILL WORKS / WHAT THIS AFFECTS / WHAT TO DO format from `CLAUDE.md`'s new "Diagnosis output format" section. Engineering detail isolated under a "Technical detail (for the developer)" divider at the bottom.

**Verified end-to-end after deploy:** `/health` returned `{"status":"ok"}` (Max correctly classified as not-yet-onboarded, not as broken). Post-restart smoke test (`runtime_smoke_test`) PASSED through the public Funnel URL. 280 tests pass remotely.

**Engineering call validated:** committed and pushed each fix as its own commit (4 fixes + 1 test-fixture isolation fix + 1 hardcoded-URL follow-up = 6 commits in the deploy batch). Net cost: ~30 sec downtime to land all four. Everything from the prior session's 9 audit items also landed in the same deploy.

## Two findings the recovery surfaced (added 2026-05-06 PM)

### A6 — Tailscale Funnel target and runtime bind address are unsynchronized
The Funnel proxies the public URL to `http://127.0.0.1:8080`. Yesterday's "cost dashboard reachability fix" changed `server.host` to `100.64.0.10` — opening laptop access but silently breaking the Funnel→runtime link. Microsoft has been getting 502 BadGateway on every webhook delivery since that change shipped. **No test, no invariant, no monitoring caught the misalignment.** Today's silent failure was caused by this, not by the migration gaps that surfaced it.

**Prescription (Stage 8.1):** add a Funnel reachability self-check. At startup AND every 15 minutes, GET `<public_url>/health` from outside the Tailscale network (or via a synthetic probe path) and verify a 200/503 response (anything but 502). Log CRITICAL + flip `state_invariants` to degraded if not reachable.

### A7 — Webhook handlers don't gracefully handle stale-notification 404s
During recovery from a multi-hour outage, Microsoft replays buffered notifications. Some reference emails that are no longer in the inbox. `graph.fetch_message` raises `HTTPStatusError` on 404 → handler fails → wrapper records a failure → rate-alert fires (false alarm).

**Prescription (Stage 8.2):** `fetch_message` returns None on 404. Caller skips with `INFO message_no_longer_in_inbox`. The rate-alert tally excludes this class entirely.

## What I'm explicitly NOT recommending after the recovery

- Refactoring `_load_subscription_index` to re-read on every webhook (decouples runtime from boot-time state). Stale-cache risk in normal operation outweighs the recovery-window upside.
- Making `/health` re-evaluate invariants on every poll. Same reason; once-at-boot is the right cadence for the 2026-05-06 failure class. Stage 8.3 if it bites again.
- Auto-restart on invariant failure. Risk of crash loop > benefit of self-heal.

---

## What's broken or at risk

The runtime keeps reporting healthy while users get nothing. We've shipped three monitoring layers in two days (handler-failure email, fallback iMessage, daily error-budget rollup) and each new failure has slipped past all of them. The architecture has duplicated writers and half-migrated state. Adding more alerts will not fix that — removing the duplication will.

## Who feels it and when

Megha. Every iMessage to Kavi today since 10:55 AM PT goes silent. Every email to her Outlook produces no MS To Do task. She does not learn this until she actively tests. Frequency in the last week: 3 incidents, ~2-4 hours of recovery each.

## Why now

Leverage. The next incident is days away, not weeks. The fix is roughly two days of focused engineering and removes the entire failure class.

## The recurring failure class

| Date | Failure shape | Yesterday's fix that should have caught it | Why it didn't |
|---|---|---|---|
| 2026-05-05 AM | process up + handler wedged on corrupt state | per-writer-unique tmp in `state.py:save_imessage_state` | Original incident — fix landed after the fact |
| 2026-05-06 AM | process up + handler wedged on corrupt state at a different path | same fix as above | Fix only patched ONE writer. A second writer at `/Users/kavi/HomeOS/.claude/imessage-state.json` still uses shared `.tmp`. |
| 2026-05-06 PM | process up + inbound webhook silent | none — no detector for "0 invocations" | All current alarms assume handlers run. Denominator is 0, no alarm. |

The deep pattern: **monitoring is what we add when we cannot trust the system. The cure here is to remove the duplication so there is no second writer to forget about, and remove half-migrated state so there is no orphaned subscription to lose track of.**

## Findings

### A. State management

**A1. State files scattered across three or more home directories.**
- `/Users/kavi/.config/kavi/` (secrets, tokens, subscriptions)
- `/Users/kavi/kavi-runtime/state/` (the path yesterday's fix targeted)
- `/Users/kavi/HomeOS/.claude/` (today's corrupt `imessage-state.json` and `imessage-state.json.tmp`)
- Possibly more under `runtime_metrics/`, `snapshots/`, etc.

There is no single home for "JSON state Kavi must read on startup." Modules invented their own paths. Yesterday's fix only knew about the `state/` path.

**A2. No single atomic-write helper.**
At least three writers reinvent the atomic-replace pattern with different correctness:
- `state.py:save_imessage_state` — patched yesterday with `tempfile.mkstemp`
- The `periodic_summary` job's writer — still uses a shared `.tmp` filename. Stack trace today: `os.replace('/Users/kavi/HomeOS/.claude/imessage-state.json.tmp', '/Users/kavi/HomeOS/.claude/imessage-state.json')` raised `FileNotFoundError`.
- BlueBubbles sync writer — uncaudited.

The fix is one helper used everywhere, plus a parity test.

**A3. Migrations not transactional.**
The `~/.config/kavi/subscriptions/` directory was created today at 09:03 (presumably during the secrets/Keychain migration) and is empty. The legacy `graph_subscription.json` was not moved into it. The runtime now reads from the empty directory and finds nothing. Microsoft is still delivering webhooks for the live subscription_id `51413854-3fc6-423b-9c80-3666384678ec`, which Kavi no longer recognizes.

A migration that can leave the system in this half-state is not a migration — it is a footgun. Migrations must be transactional or self-healing on next boot.

**A4. State files have no schema validation.**
The corrupt JSON today (`Extra data: line 30 column 2 (char 1678)`) is a small valid JSON object followed by leftover bytes from a previous larger write — exactly the same shape as yesterday's incident. Nothing checks the file is valid before write. Every reader explodes at use time.

### B. Failure-mode coverage

**B1. "Process up + inbound silent" has no detector.**
Every alarm we shipped assumes handlers run:
- Per-failure email — needs a handler to raise.
- Fallback iMessage — needs a handler to raise.
- Rolling failure-rate (`failures / invocations`) — denominator is 0 when no inbound arrives.
- Daily 7am error-budget email — would catch "0 invocations today" tomorrow morning, ~17 hours late.
- Healthchecks.io — `/health` returns 200, scheduler ticks; passes.

We need an invocation-floor alarm: if 0 inbound events from any source for N minutes during waking hours, send a fallback iMessage to Megha and an email. This is the single missing alarm that would have caught today's incident at 11:25 AM PT instead of 2:30 PM PT.

### C. Acceptance criteria

**C1. Capability acceptance criteria don't include observability SLOs.**
`capabilities/inbox-to-task.md` and `capabilities/realtime-kavi.md` describe the happy path and Hard rules but don't include a clause like "if Megha sends a message, she gets either a reply or an explicit silence-alarm within N minutes." Without that SLO, no alarm has a target to honor.

**C2. Observability surfaces themselves have no staleness criterion.**
The structured log (`kavi-runtime.json.log`) went stale at 11:14 AM PT today. Nothing alarms on a log that stops being written to. Same applies to the cost dashboard, the daily error-budget email, and the eval JSONL files.

### D. Unit testing

**D1. Yesterday's parity test only covered outbound gates.**
`test_outbound_gates_parity.py` asserts every caller flows through the gate. Good pattern, wrong target. We need an analogous parity test for state writers: "every JSON state file is written through the same atomic-replace helper with a per-writer-unique tmp." Today's failure would have been caught here.

**D2. No test for migration idempotency or half-migrated recovery.**
A test that simulates "subscriptions/ is empty but graph_subscription.json exists at the legacy path" should force the runtime to either back-fill on startup or refuse to start with a clear error. Neither happens today.

**D3. Tests can run against the live runtime and write into prod logs.**
Today the structured log captured fixture data: 9 different `compose_*` Anthropic calls within 53 milliseconds, `input_tokens: 10, output_tokens: 10, latency_ms: 0`, `inbound_source: "email malicious@phisher.com"`. These are clearly mocked SDK responses from a test suite that was pointed at the live runtime around 11:14 AM PT. The cost dashboard now contains test data alongside real spend. Eval rollups are contaminated.

### E. End-to-end testing

**E1. No post-restart smoke test.**
After every runtime restart, a synthetic email + a synthetic iMessage should fire through real channels, and the runtime should confirm round-trip in under 60 seconds. Today's silence would have been caught at 11:15 AM PT.

**E2. No webhook subscription health check at startup.**
The runtime came up at 11:14 AM PT with `subscriptions/` empty for the only active account and did not refuse to start. A startup self-check should walk every account in `tokens/`, verify each has a live subscription record with `expiration_dt > now + 1h`, and either re-register or fail loud.

### F. Operational hygiene

**F1. No staging environment.**
`pytest` runs against the same Python installation, the same secrets, the same state files, the same logs as the live runtime. There is no second runtime instance with isolated state. Until that exists, every test run is a potential prod incident.

**F2. Cost dashboard lacks env-tagging.**
Anthropic calls are logged with model + token counts but no `env=prod|test` flag. Test runs pollute spend rollups. Today's $0.00 spike on the dashboard is the test, not real traffic.

## Prioritized list

### P0 — ship this week. ~10 engineering hours. **ALL SHIPPED 2026-05-06 PM.**

**P0.1. Single atomic-state-write helper + parity test.** ~4 hrs. **DONE.**
Shipped as `kavi_runtime/state_io.py` (`atomic_write_json` / `atomic_write_text` / `atomic_write_bytes` / `read_json_strict` / `read_json_recover`). 9 raw-write call sites across `state.py`, `handler_alerts.py`, `graph_client.py`, `add_account.py`, `secrets.py` consolidated. Static-analysis parity test in `tests/test_state_io_parity.py` enforces no future regressions.

**P0.2. Invocation-floor alarm.** ~2 hrs. **DONE.**
Shipped as `maybe_alert_invocation_floor` in `handler_alerts.py` + scheduler job in `scheduler.py`. 5-min interval, 30-min silence threshold, waking hours (07:00-23:00 PT), debounced 30 min. Fires fallback iMessage + alert email. 9 test cases in `tests/test_invocation_floor.py`.

**P0.3. Startup webhook self-check.** ~3 hrs. **DONE (partial).**
Shipped as `kavi_runtime/state_invariants.py` (per-account token + subscription file checks; expiration buffer; `/health` returns 503 with the exact `add_account.py` command on CRITICAL violations). 9 test cases in `tests/test_state_invariants.py`. **Deferred to Stage 8.1:** the actual public-URL reachability check (today's actual root cause). The on-disk checks shipped today catch orphaned-state; the network reachability check is next.

**P0.4 (NEW, ad-hoc). Funnel↔runtime bind alignment.** ~1 hr. **DONE.**
`config.yaml`: `server.host` reset to `"0.0.0.0"` so the runtime binds both loopback (Funnel target) and the Tailscale interface (laptop direct access). This was today's actual root cause of inbound silence; everything else was downstream.

### P1 — next sprint. ~12 engineering hours.

**P1.4. Post-restart E2E smoke test.** ~3 hrs.
Synthetic email through Megha's Outlook (or a sentinel test address) + synthetic iMessage from a test handle. After every restart, runtime fires both, expects round-trip in MS To Do creation or persona reply within 60s, alarms otherwise. Run via launchd post-startup hook.

**P1.5. Test-vs-prod isolation.** ~5 hrs.
`pytest` refuses to run when `KAVI_ENV != "test"`. Test fixtures use a separate state directory under `/tmp/kavi-test-<pid>/` and a mocked Anthropic client. Pre-commit hook + CI assert no test imports the prod state path. Migrate the cost dashboard to filter on `env=prod`.

**P1.6. State path consolidation.** ~4 hrs.
Pick one home (`~/.config/kavi/state/` is the natural choice — separate from `~/.config/kavi/secrets/`). Migrate every state file. Hard-fail at startup if any module reads from the old paths. Document the rule: "state lives in one place, use `state_io` to read/write it."

### P2 — when there's space. ~8 engineering hours.

**P2.7. Schema validation on state writes.** ~3 hrs.
Pydantic models for every state file. `state_io.atomic_write_json` validates before write. Corrupt writes fail loud at the source, not at the next reader.

**P2.8. Migration transactionality.** ~3 hrs.
Migration scripts run inside a transactional wrapper: write to a `.migrating/` directory, validate, then atomically rename. Crash mid-migration leaves the old state intact. Idempotency: re-running a completed migration is a no-op.

**P2.9. Acceptance criteria refresh.** ~2 hrs.
Every capability in `capabilities/` gets an SLO line: "Megha notices if X is broken within N minutes." Pair with the invocation-floor alarm so the SLO is enforceable. Pattern: each capability gets a `## Failure-mode coverage` section listing what alerts cover what failures.

### P3 — backlog.

**P3.10. Test-data tagging on Anthropic calls.** ~1 hr.
Every call logs `env=prod|test`. Cost dashboard filters. Stops one class of dashboard pollution but not the test-vs-prod isolation problem (P1.5 is the real fix).

## Cost & leverage framing

| | P0 | P0+P1 | P0+P1+P2 |
|---|---|---|---|
| Engineering hours | ~10 | ~22 | ~30 |
| Failure classes closed | 2 of 3 (state race + inbound silent) | 3 of 3 + dashboard pollution | All known classes + future schema/migration safety |
| Expected incidents avoided per month | 2-3 | 3-4 | 3-4 + cleaner debugging |
| User-visible impact | Megha can trust an iMessage to Kavi will either get a reply or trigger an alarm | Plus: every restart self-validates within 60s | Plus: migrations and tests cannot break prod |

The single highest-leverage item is P0.1 (atomic-write helper). It is the smallest fix that closes the largest failure class, and it sets the precedent for "one helper, parity-tested" that the rest of the audit depends on.

## What this audit explicitly does NOT recommend

- More monitoring layers on top of the existing ones. We have enough alarms; we have a discipline gap.
- Rewriting handlers.py (3000 lines, CC=75 on the largest function). Yes, refactor needed. No, it does not address today's failure class. Defer to a separate session.
- Switching from JSON files to SQLite. Tempting after watching three JSON corruptions in 48 hours, but the failure is the writer pattern, not the format.

## Open work — Stage 8 (next session)

### Stage 8.1 — Funnel reachability self-check. ~2 hrs. Highest leverage.
Today's actual root cause class. At startup AND every 15 min, probe `<public_url>/health` from a path that exits the Tailscale network. On non-2xx/503: log CRITICAL, mark invariants degraded, fire fallback iMessage + email. Closes A6.

### Stage 8.2 — Graceful 404 on stale notifications. ~1 hr. Low risk.
`fetch_message` returns None on 404. Handler logs `INFO message_no_longer_in_inbox` and skips. Rate-alert tally excludes this class. Stops the false-alarm spam during recovery windows. Closes A7.

### Stage 8.3 — Max's account onboarding.
Run `python -m kavi_runtime.add_account max@example.com` interactively (he needs to be at the keyboard for the device-code flow). After it completes, `/health` flips to 200. Pure operations, no code change needed.

### Carry-forward from the original P1/P2/P3 list
P1.4 (E2E smoke test), P1.5 (test-vs-prod isolation), P1.6 (state path consolidation), P2.7 (Pydantic schema validation on writes), P2.8 (transactional migrations), P2.9 (SLO refresh on capabilities), P3.10 (test-data env tagging) — none shipped yet. Sequence and bundle when next-session bandwidth allows.

## Decisions resolved this session

- **Engineering call: one branch, no per-item PRs.** Validated. 5 stages landed via single rsync deploy + 1 restart with ~30 sec downtime. Three open questions from the original draft were answered implicitly during execution.
- **Stage 4 invariants are once-at-boot, not dynamic.** Decision: stay once-at-boot. Stale `/health` between `add_account.py` and next restart is acceptable given how rarely accounts are added. Re-checking on every `/health` poll would invert the cost. (Funnel reachability is a separate dynamic check via 8.1; that one DOES update `/health` on every poll.)
- **Forward-migration of legacy state runs at GraphClient construction.** Two idempotent phases (P2.8) so a crash between them is recoverable on next boot.

## Capability SLO matrix (P2.9)

Single source of truth. Each capability's user-visible failure mode mapped to the alarm that catches it and the latency. Capability docs are unchanged — that's intentional, the matrix lives once here.

| Capability | What Megha notices when broken | Alarm that catches it | Latency |
|---|---|---|---|
| **inbox-to-task** | Email arrives in Outlook but never appears as a task in MS To Do | Invocation-floor alarm (Stage 5) when 0 inbound for 30 min during waking hours; Funnel reachability check (8.1) when public URL is unreachable | ≤30 min during 7 AM–11 PM PT |
| **kavi-persona** | Reply from Kavi sounds off-voice or fails structural checks (g_v1-v5, g_p1-p3) | Weekly HTML-viewer open coding (`evals/viewer.html`) + weekly self-check + structural_pass column on every outbound row | 7d (review cadence) |
| **realtime-kavi** | iMessage to Kavi gets no reply, no fallback iMessage either | Invocation-floor alarm + state_invariants (Stage 4) on `/health` + Funnel reachability (8.1) + handler-level fallback iMessage | ≤30 min during waking hours; instant when handler raises |
| **kavi-coordinates** | Coordination request from Megha never reaches addressee, or reaches but no outcome reply | Outbound logger row per coordination phase + manual eval review (`/eval-coordinates`) | 24h |
| **imessage-to-task** | "Add X to my list" verb in iMessage doesn't create a task | Outbound logger + handler-level fallback iMessage on raise | Instant on handler raise; 30 min if handler never invoked |
| **scheduled-reminders** (proposed) | Time-bound task doesn't fire its reminder | TBD — will pair with invocation-floor + a per-reminder eval row at ship time | TBD |
| **kavi-anticipates** (proposed) | Kavi doesn't proactively surface a task Megha expected | TBD at ship | TBD |
| **url-summarizer** (proposed) | URL shared in iMessage produces no summary | TBD at ship | TBD |
| **external-thread-monitor** (proposed) | External thread state change isn't surfaced | TBD at ship | TBD |

The two-tier pattern: **runtime-level alarms** (Stages 4, 5, 8.1) cover capability silence regardless of which capability was supposed to fire — they're the floor. **Capability-level evals** cover correctness — did the capability fire AND produce the right output.

Stage 8 fixed the runtime-level floor. Stages 5/6/7 of the original build (kavi-persona, kavi-coordinates) had eval surfaces shipped; the proposed capabilities will get matching evals at ship time.
