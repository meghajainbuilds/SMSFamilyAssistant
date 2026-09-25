# Engineering discipline

Living doc. The bar HomeOS engineering work holds itself to. Captures lessons from past incidents and codifies the discipline going forward. Update whenever a recurring failure mode is named.

---

## Why this doc exists

HomeOS is a one-Mac, two-user, low-traffic system. It is also Megha's flagship AI-native build during her interview prep window. The bar is not "ships features"; the bar is "happy-path and core flows are reliable enough that Megha can talk about this build with conviction in a Director-level interview." Reliability has to come from process and systems, not heroics.

The discipline below is the bar. The post-mortem after it is the evidence that the bar has not always been met.

---

## Discipline rules going forward

These are the rules every fix bundle (whether built by a human, an agent, or both) must follow. Codified after the 2026-05-08 mark-done debug cycle.

### 1. Pull the full evidence before prescribing a fix

Every fix proposal must start with four artifacts in hand, not three:

- The full production trace (inbound, runtime decision path, outbound)
- The full err log around the failure timestamp (not the truncated warning text)
- The actual LLM response that broke (raw output, not the 300-char log line)
- The actual external-system state (MS Graph, BlueBubbles, MS To Do)

If any one is missing, the fix proposal is a hypothesis, not a diagnosis. Mark it as such; do not ship.

### 2. Smallest-possible-fix rule

Default to the smallest change that addresses the root cause. Any fix that introduces new infrastructure (new tokenizer, new state field, new gate, new cache) requires explicit justification of why an existing layer cannot do the job. The bar: the explanation must answer "what does the existing LLM judgment / API call / data structure already do that we are duplicating?"

### 3. Pattern audit on naming

When an architectural pattern is named in the codebase (good or bad), grep for every instance within the same diagnosis turn. Treat patterns as bug families, not single instances. The grep + audit appears in the same chat turn as the naming, so the pattern is not just labeled but inventoried.

### 4. User-visible acceptance criteria

Every fix bundle states acceptance in user-visible terms before coding starts:

> When Megha says X, Kavi does Y, MS To Do shows Z within N seconds.

Tests are a proxy. The acceptance criterion is the user-visible outcome, full stop.

### 5. /health is a liveness check, not a correctness check

A 200 OK on /health proves the process is up. It does not prove the user-visible flow works. A deploy is not "verified live" until a fixture iMessage has been simulated against the running runtime and the expected outcome has been asserted on real external systems (MS Graph, BlueBubbles).

### 6. Defense-in-depth is a safety net, not a primary defense

G-A1, phrase-ban lists, fail-closed flags, structural checks: these catch the rare miss after correct logic. They are not the first line of defense. The first line is correct logic up front. If a fix bundle leans on a safety net to compensate for unverified primary logic, the bundle is incomplete.

### 7. Bundle sizing

One root cause per commit. One commit per deploy. One user-visible verification per deploy. Bundles that couple multiple fixes prevent rollback on partial failure and stack context risk per cycle.

### 8. Roll-forward only on green

When a deploy fails verification, the response is `git revert` + redeploy + diagnose-on-revert state. Never patch on top of unverified state. Do not stack fixes on top of broken.

### 9. Production traces are the spec

Every production failure becomes a permanent test fixture (see Investments §1) before the fix ships. Failures are gold; they are the empirical specification of the system's edge cases.

### 10. Sub-agent contract enforcement

Agent prompts state the deploy contract (what to rsync, what to verify, how to roll back). Agent reports are not verification; verification is what the deploy script asserts. If the contract is in the prompt but not in the verification, it is aspirational, not enforced.

---

## Investments to build (the next-cycle backlog)

Ordered by leverage. Build in this order unless capability work explicitly trumps.

### Investment 1 — Production-trace regression library

Every production failure becomes a permanent test fixture before the fix ships. File path pattern: `kavi-runtime/tests/regression/test_<date>_<short_desc>.py`. Each fixture freezes:

- The inbound text (verbatim)
- The MS Graph state at failure time (snapshot of relevant tasks)
- The LLM response that broke parsing (raw, full)
- The expected user-visible outcome

Build cost: low. ~2 hours to set up the directory, the helper that loads fixtures, and the first 3-5 fixtures pulled from tonight's session. Then ongoing: one fixture per future production incident, written before the fix.

Leverage: every future fix runs this library. The same failure mode cannot recur silently.

### Investment 2 — Deploy verification + auto-rollback (SHIPPED)

`kavi-runtime/scripts/deploy.sh` and `kavi-runtime/scripts/verify_deploy.sh` are the canonical deploy + verify pair. Use them for every deploy.

`deploy.sh`:

1. Reads current remote `.deploy_sha` (the prior deployed SHA).
2. Rsyncs `kavi_runtime/`, `tests/`, `skills/` to Kavi (skills/ was the missed contract item in the 2026-05-08 post-mortem; no longer optional).
3. Clears `__pycache__`, kickstarts launchd, writes the new `.deploy_sha`.
4. Runs `verify_deploy.sh`. On failure, auto-rolls-back to the prior SHA via a temp git worktree + re-rsync, then re-runs verify.

`verify_deploy.sh`:

1. SHA match between local HEAD and remote `.deploy_sha`.
2. Recursive sha256 hash match between local and remote `kavi_runtime/`, `tests/`, `skills/`. Skill files cannot be silently absent.
3. `/health` returns `{"status":"ok"}` over loopback on Kavi.
4. Smoke fixture: POSTs a neutral-text BlueBubbles webhook payload (with a unique marker in the inbound text), polls `/evals/kavi-persona/recent` for up to 30s, asserts an outbound row with the marker landed. Proves the inbound -> classifier -> composer -> outbound-log -> send chain is alive without mutating MS To Do state.

Bootstrap (one-time): on first deploy, the runtime won't have `.deploy_sha`. Either run `ssh kavi "echo <sha> > /Users/kavi/kavi-runtime/.deploy_sha"` once, or run `SKIP_VERIFY=1 ./scripts/deploy.sh` to seed the file and then a normal `./scripts/deploy.sh` to verify.

Sub-agent contract (rule §10): every agent that ships a runtime change MUST run `./scripts/deploy.sh` (not raw rsync). Agent reports of "deployed + /health green" are no longer accepted as verification — the verifier is what asserts the contract.

### Investment 3 — Staging environment

A second runtime instance pointing at a separate test MS To Do list. Every code change deploys there first; verify_deploy.sh runs against fixture inputs; only then does the change deploy to prod.

Build cost: high. Requires (a) a staging Apple ID with BlueBubbles, (b) a separate MS account with its own tasks list, (c) a `STAGE=staging|prod` switch in config, (d) a separate launchctl service.

Leverage: staging is the highest single-investment leverage available. Every deploy gets a real-data dry run before reaching production. Skill-file misses, max-tokens clips, apostrophe edge cases — all surface in staging, not prod.

Defer until Investment 1 + 2 are landed; a staging environment without verification is just a second prod.

### Investment 4 — Live LLM-output snapshot tests

Tests today mock the LLM. Every change to a system prompt or skill should also run a non-mock test that calls the real Anthropic API with fixture inputs and asserts response shape (parses cleanly, fits expected schema, output_tokens within window). Run nightly via cron.

Build cost: low. ~2 hours. Reuses regression library fixtures.

Leverage: catches the failure mode where a prompt change makes the LLM output drift in shape (e.g., starts wrapping in code fences). Tonight's `output_tokens=400` clip would have surfaced here weeks before it bit production.

### Investment 5 — Pattern audit cron

Once a quarter, grep the codebase for known anti-patterns: `[:N]` slices in front of LLM calls, deterministic `re.search` filters between LLM steps, hardcoded timeouts in milliseconds. Build a checklist that the audit runs against. Append findings to this doc.

Build cost: low. ~1 hour to define the patterns + cron the grep.

Leverage: prevents the "I named the pattern then forgot to grep" failure mode from tonight.

---

## Post-mortem: 2026-05-08 mark-done debug cycle

Six commits over four hours to fix one user-visible flow ("Mark all elders tea tasks done"). Three rounds of "this should work now" → Megha tests → fails differently → diagnose → patch. The user-visible outcome only landed on the seventh attempt. Pattern of failure: each diagnosis was correct given the evidence collected, but evidence was consistently under-collected before fixes were prescribed.

### The actual misses, in order

1. **Wrong root cause on Bundle 1.** Diagnosed the short-text-ack shortcut as the cause of "Mark all elders tea items done" failing. But that inbound was 30 chars — a full sentence — never going through short-text-ack. Read the failure mode without reading the actual code path. Bundle 1 shipped a fix for a problem that was not blocking.

2. **Over-engineering on Bundle 2.** Directed the agent to add a deterministic topic pre-filter. The matcher LLM could already do this. Added complexity that introduced an apostrophe-tokenization bug.

3. **Sub-agent reports were trusted as verification.** Agents reported "tests pass, /health ok, deployed." None of those mean "the user-visible flow works." No agent ran a real iMessage simulation against the live runtime.

4. **Skill files silently absent for 24+ hours.** Every fix-bundle agent rsync'd `kavi_runtime/` and `tests/` but skipped `skills/`. The contract said "rsync runtime + tests + skills." Agents did not follow the contract. Nothing checked.

5. **Latent bugs masked by smaller load.** `max_tokens=400` and `[:30]` slice on the matcher input were both pre-existing bugs that only surfaced when today's fixes raised the slate size. No stress testing of the matcher with realistic input sizes.

6. **Diagnosis layer-by-layer instead of full evidence first.** When the parser failed, patched it; when patching did not help, raised max_tokens. Should have pulled the FULL LLM response on the first parse failure. The truncated 300-char warning hid the real issue (response clipped mid-JSON, not malformed JSON).

7. **No audit when a pattern was named.** Named "deterministic narrowing before LLM judgment" as a pattern, wrote it into architecture.md, then did not grep the codebase for other instances. The `[:30]` matcher slice was discoverable with one shell command before Megha ran the failed test.

### Root causes

- **Prescriptive instead of diagnostic.** Jumped to "here is the fix" before fully characterizing the failure.
- **/health is liveness, not correctness.** No integration smoke-test layer between unit (mocked) and production (Megha).
- **Defense-in-depth treated as primary defense.** G-A1, phrase ban, fail-closed — leaned on safety nets that had gaps.
- **Production traces not treated as the spec.** Every failure trace contained the answer; none became regression fixtures.
- **Bundle size too large per deploy.** Each bundle had 3-5 coupled fixes; rollback impossible on partial failure.

### What was on the orchestrator vs what is on the codebase

**On the orchestrator:**
- Reading evidence layer-by-layer instead of fully before prescribing
- Trusting agent self-reports as verification
- Not pattern-auditing when patterns were named
- Bundling fixes too large

**On the codebase:**
- No staging environment
- No deploy verification beyond /health
- No production-trace regression library
- No real-LLM-output snapshot tests

Both halves need work. Systems-level investments (staging, deploy verification, regression library) catch errors regardless of orchestrator discipline. Orchestrator discipline (pull evidence first, smallest fix, pattern audits) prevents the misses systems cannot see.

---

## Pattern audit findings

Run via `python kavi-runtime/scripts/pattern_audit.py` (Investment 4, shipped 2026-05-08). The script greps for the bug families this doc names and prints a markdown report. Each finding is a candidate for review, not an automatic verdict.

### First run (2026-05-08)

Section totals against `kavi-runtime/kavi_runtime/`:

| Section | Matches | Notes |
| --- | --- | --- |
| Slice caps before LLM calls | 139 | Most are log-truncation slices (`title[:80]` on log lines); the production-relevant ones are in `claude_client.py` (matcher input cap `[:100]`, candidate trim `[:30]`, etc.). High recall, low precision — every match needs human eyeballs. |
| Regex filters between LLM calls | 2 | Low signal; review at next pattern-audit cycle. |
| Hardcoded max_tokens literals | 21 | Includes the canonical `max_tokens=1500` in `match_target_to_open_task` (post-fix) plus 20 inline literals across composers + classifiers. Consolidate into `max_tokens_routing` config or named module constants. |
| Hardcoded timeout literals | 25 | `timeout=30.0` on every Graph call, `timeout=10` on BlueBubbles loopback. Move to a single module constant per integration. |
| Hardcoded Graph paging caps | 4 | `$top=50` (line 487, 686), `$top=25` (line 575, 591) in `graph_client.py`. Should be named constants so the next "open-task slate dropped older items" bug is reviewable in one place. |
| Bare except / except-pass | 0 | Clean. |

**Canonical highlights (named in the 2026-05-08 post-mortem):**

- `claude_client.py:1159` — `for t in (open_tasks or [])[:100]:` (matcher input cap, post-cef1817 fix).
- `claude_client.py:1485` — `for t in (open_tasks or [])[:30]:` (clarifying composer trim — same bug class).
- `claude_client.py:1206` — `max_tokens=1500,` (post-6015776 fix; was 400, clipped JSON mid-response).
- `graph_client.py:487` — `params={"$top": 50},` (recency-only fetch, status-agnostic; same paging-cap class as the 2026-05-08 30-task miss).

Action: review these at the next pattern-audit cycle (quarterly per the original Investment 5 cadence). Track follow-ups in `kavi-runtime/backlog.md`, not here.

---

## How to use this doc

Read it before kicking off a fix bundle. Read it before approving an agent's deploy report. Read it after a production incident, then update the discipline rules and investments backlog with what was learned.

Last updated 2026-05-08 (Investment 4: pattern audit script + first-run findings).
