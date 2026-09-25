---
description: L1 eval surface for the Kavi coordinates capability. Fetches today's coordination sessions from Kavi's runtime, renders one labelable session block per session in chat (multi-phase rows grouped by session_id), accepts a one-line reply with binary annotations + free-form notes, and appends rows to evals/kavi-coordinates/eval-coordinates-labels.jsonl.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
argument-hint: "(no arguments) | --date YYYY-MM-DD"
---

> **Kavi's address:** `100.64.0.10` and `kavi-mac.your-tailnet.example` below are public placeholders. Before any ssh or curl, read `CLAUDE.local.md` at the repo root (gitignored) for the real values and substitute them.
# /eval-coordinates — read coordination sessions, label them

Reads every coordination Kavi ran today (or any specified date), renders one labelable block per session (multi-phase rows grouped together so the whole loop is visible at once), accepts Megha's per-session labels in a single reply, and persists annotations to a local append-only JSONL.

This is the L1 eval surface for the coordination capability per `evals/ritual.md`. Without this command, multi-phase coordination judgments accumulate on Kavi unobserved and the close-rate / attribution / leakage metrics stay aspirational.

Unit of analysis: **one coordination session**, not one phase row. Multiple judgment rows in `eval-coordinates-judgments.jsonl` share a `session_id`; one label row in `eval-coordinates-labels.jsonl` scores the whole session.

## Inputs

- `--date <YYYY-MM-DD>` — optional. Default: today (Pacific date, matches the runtime's local clock).
- No other args. (Optional `--limit <N>` may be added later; v1 hard-caps render to 10 sessions per call.)

## Procedure

### 1. Resolve target date

If `--date` arg is present, use it (must match `YYYY-MM-DD`).
Otherwise, set `target_date` to today in Pacific time.

### 2. Fetch coordination judgment rows from Kavi's runtime

The runtime exposes a read-only endpoint at `127.0.0.1:8080/evals/kavi-coordinates/recent`. The endpoint is bound to localhost on Kavi, so we shell out via SSH (the SSH key is already authorized — see `handoffs/handoff-2026-04-29.md` step 2).

```bash
JUDGMENTS=$(ssh kavi@100.64.0.10 "curl -s --max-time 5 'http://127.0.0.1:8080/evals/kavi-coordinates/recent?date=<target_date>&limit=50'")
```

Parse `$JUDGMENTS` as JSON. Capture `rows` array. Each row has the schema from `evals/definitions.md` → `eval-coordinates-judgments.jsonl`:
- `decision_id`, `session_id`, `ts`, `phase`
- `requester_handle`, `addressee_handle`
- `inbound_text` (only on the `ack` phase row), `ack_text`, `ack_latency_sec`
- `addressee_message_text`, `addressee_message_attribution`
- `addressee_reply_text`, `addressee_reply_latency_sec`
- `branch`, `task_created`, `task_id`
- `outcome_report_text`, `outcome_report_latency_sec`
- `closed`, `usage`

If `rows` is empty: print `"No coordination sessions for <target_date>. Either Kavi didn't coordinate anything that day, or the runtime isn't writing yet."` and exit cleanly.

If the SSH/curl fails: print `"Couldn't reach Kavi's runtime at 100.64.0.10:8080. Check (a) Kavi is online (Tailscale up), (b) the kavi-runtime process is running (ssh kavi@100.64.0.10 'pgrep -fl kavi-runtime')."` and exit.

### 3. Group rows by session_id

The judgments endpoint returns one row per phase. Build a map `session_id → [rows]`, sorted by `ts` within each session. Order sessions for display by the earliest `ts` per session (chronological).

For each session, derive these display fields by walking the phase rows:
- `requester_handle` (from any row)
- `addressee_handle` (from `addressee_reach` row, if present)
- `inbound_text` (from `ack` row)
- `ack_text`, `ack_latency_sec` (from `ack` row)
- `addressee_message_text`, `addressee_message_attribution` (from `addressee_reach` row)
- `addressee_reply_text`, `addressee_reply_latency_sec` (from `addressee_reply` row)
- `branch` (from `branch_decision` row)
- `task_created`, `task_id` (from `branch_decision` or whichever row sets it)
- `outcome_report_text`, `outcome_report_latency_sec` (from `outcome_report` row)
- `closed` (true if any row has `closed: true`, else false)
- `in_flight` flag = NOT closed AND no `outcome_report` row yet AND no `escalated_to_requester` row

Resolve `requester_handle` and `addressee_handle` to human names via `household.md` if straightforward (Megha / Max / Kavi). If a handle isn't matchable, render the raw phone handle.

### 4. Read existing labels

Read `/Users/meghajain/Documents/HomeOS/evals/kavi-coordinates/eval-coordinates-labels.jsonl` line by line if it exists. Build a map `session_id → most_recent_label` (most recent by `annotated_at`). For each session, if `session_id` is in the map, the session has already been labeled. Carry that label forward as a pre-fill.

### 5. Render session blocks in chat

Hard cap: render up to 10 sessions per call. If more, render the 10 most recent and print `"<P> additional sessions for this date — re-run after labeling these."` at the end.

Heading line:
```
# Coordination sessions — <date> (<N> sessions; <K> pre-labeled; <F> still in flight)
```

For each session, render in chronological order:

```markdown
### Session <i> — `<session_id>`

**Requester (<requester_name> → Kavi):** "<inbound_text>"

**Kavi → <requester_name> (ack, <ack_latency_sec>s):** "<ack_text>"

**Kavi → <addressee_name> (with attribution):** "<addressee_message_text>"
   _(or "(without attribution)" if `addressee_message_attribution == false`)_

**<addressee_name> → Kavi (<addressee_reply_latency_sec formatted>):** "<addressee_reply_text>"
   _(or "Still waiting for reply." if `addressee_reply_text` is null and session is in flight)_

**Branch:** <branch_label>. **Task created:** <yes / no, with task_id if yes>.

**Kavi → <requester_name> (outcome, <outcome_report_latency_sec>s):** "<outcome_report_text>"
   _(or "Outcome not yet reported." if outcome_report row is missing)_

**Already labeled:** <Yes — show pre-filled label values / No>.
```

Formatting rules:
- `addressee_reply_latency_sec`: render in human form (`<60` → `Ns`; `60-3600` → `N min`; `>3600` → `N h M min`).
- `branch_label`: human translation of the branch code. `4a_yes_have_it` → "yes-have-it". `4b_will_grab` → "will-grab". `4c_ambiguous` → "ambiguous". `4d_no_reply` → "no-reply". For other / future branch values, render the raw value.
- If a phase is missing because the session is still in flight, render the phase line with the `(Still waiting...)` / `(Outcome not yet reported.)` placeholder. Do NOT skip lines silently — the absence is the signal.
- If `addressee_message_text` is null (e.g., session_failed before reach), render `**Kavi → addressee:** (no message sent — session failed at <phase>)` and skip downstream phase lines.

After all session blocks, print the reply prompt block (see step 7).

### 6. Write the persistent markdown view

Save heading + all session blocks + reply prompt verbatim to `/Users/meghajain/Documents/HomeOS/evals/kavi-coordinates/eval-coordinates-<target_date>.md`. Overwrite if exists. Disposable derived view; the .jsonl files are source of truth.

### 7. Reply prompt

```
## Reply format

One line per session. Compact letter codes (case-insensitive), optional quoted note at end.

  <session_index>: cl=<y/n> fu=<y/n/null> at=<y/n> fp=<y/n> il=<y/n> ["free-form note"]

Codes:
  cl  →  close_rate_pass         (Kavi closed the loop without requester re-pinging)
  fu  →  follow_up_window_correct (timing on the addressee follow-up; null when no follow-up needed)
  at  →  attribution_correct     (right call on attribute-vs-omit per principle 1)
  fp  →  false_positive_task     (a task was created and it was unnecessary; only relevant when task_created)
  il  →  internal_ops_leakage    (outcome report narrated internal mechanics)

Examples:
  1: cl=y fu=null at=y fp=n il=n "clean cash flow"
  2: cl=n fu=n at=y fp=n il=y "narrated set-a-reminder; should have been outcome-only"
  3: cl=y fu=y at=y fp=n il=n
  4: skip "in flight, label later"

Special tokens:
  <i>: skip ["why"]   →  no label written this run (e.g., session still in flight)
  <i>: pass            →  cl=y fu=null at=y fp=n il=n (every dim passes; null fu)
  submit / done        →  end the labeling session

When a code doesn't apply (no follow-up was needed; no task was created; no outcome report yet),
use `null` (or omit the code — it will write null).
```

### 8. Wait for Megha's reply

The next user message is the labeling reply. Parse it.

### 9. Parse the reply

Recognized tokens:
- `<i>: cl=<v> fu=<v> at=<v> fp=<v> il=<v> ["free-form note"]` — full or partial label set for session `<i>`. Values: `y` (true), `n` (false), `null` (N/A). Omitted codes default to `null`.
- `<i>: pass` — shorthand for `cl=y fu=null at=y fp=n il=n` and no note.
- `<i>: skip ["why"]` — no row written for this session this run; quoted reason captured as a runtime note (logged in chat, not persisted).
- `submit` / `done` — end the labeling session.
- Free text or unrecognized — flag as parse error: `"Couldn't parse: <token>. Use '<i>: cl=y fu=null at=y fp=n il=n [\"note\"]' or '<i>: pass' or '<i>: skip'. Type 'submit' when done."` Do NOT auto-guess.

After parsing, every session that received a real label (not `skip`) gets one new row in the labels file.

### 10. Append annotation rows

For each labeled session, build:
```json
{
  "session_id": "<from session>",
  "close_rate_pass": <true|false|null>,
  "follow_up_window_correct": <true|false|null>,
  "attribution_correct": <true|false|null>,
  "false_positive_task": <true|false|null>,
  "internal_ops_leakage": <true|false|null>,
  "notes": "<free-form note or null>",
  "annotated_at": "<UTC ISO timestamp now>",
  "annotator": "megha"
}
```

Notes:
- The schema in `evals/definitions.md` uses python-style booleans (`true`/`false`) and explicit `null` for N/A. Match it exactly — don't substitute the string `"null"`.
- Required vs nullable per the schema:
  - `close_rate_pass` — required (always true/false; not nullable).
  - `follow_up_window_correct` — nullable (null when no follow-up was needed).
  - `attribution_correct` — required when an addressee message was sent; null otherwise.
  - `false_positive_task` — required when `task_created == true` in the corresponding judgment row; null otherwise.
  - `internal_ops_leakage` — required when an outcome report was sent; null otherwise.

Append to `/Users/meghajain/Documents/HomeOS/evals/kavi-coordinates/eval-coordinates-labels.jsonl`. Atomic append (open in append mode, write each line, flush). If the file doesn't exist, create it.

### 10b. Re-render the persistent markdown view

After labels are appended, regenerate `/Users/meghajain/Documents/HomeOS/evals/kavi-coordinates/eval-coordinates-<target_date>.md` (overwrite). The regenerated file uses the same session blocks from step 5, but each block's "Already labeled" line now reflects the most-recent annotation per `session_id`. Heading line updates to `# Coordination sessions — <date> (N sessions; M labeled; F in flight)`. The disposable view always reflects current state; never edit it by hand.

### 11. Print summary

```
Labels written for <target_date>:
  close_rate_pass:           X / N sessions passed
  follow_up_window_correct:  Y / Z sessions where follow-up was needed
  attribution_correct:       A / B sessions with addressee message
  false_positive_task:       C / D sessions where a task was created
  internal_ops_leakage:      E / F sessions with outcome report

Total labeled sessions for this date now: G / N
Total labels in eval-coordinates-labels.jsonl: K (across all dates)
```

If any sessions are still unlabeled OR in flight:
```
<P> sessions still unlabeled (<F> in flight). Run /eval-coordinates again to finish.
```

## Edge cases the implementation must handle

1. **Empty rows for date.** Exit cleanly per step 2; do not write an empty markdown view.
2. **Sessions still in flight.** A session with an `ack` and `addressee_reach` row but no `outcome_report` row yet renders with `Outcome not yet reported.` placeholders. Megha should typically `skip` these — re-run later when closed. Do NOT attempt to label `close_rate_pass` on an in-flight session.
3. **Missing phase rows mid-session.** Session has `ack` and `outcome_report` but no `addressee_reach` (e.g., requester answered their own question before Kavi reached out). Render the addressee block as `**Kavi → addressee:** (no addressee message — session resolved before reach).` `attribution_correct` is null.
4. **`session_failed` phase.** A session that errored mid-flight has a `session_failed` row. Render that row's status in place of subsequent phases. Megha typically labels `cl=n` plus a note.
5. **`escalated_to_requester` phase.** Kavi gave up waiting and escalated back. This counts as closed for the close-rate metric (Kavi did report back; the requester didn't have to re-ping). Render the escalation message in the outcome block. `cl` is judgment-based per the labeler.
6. **Pre-labeled sessions.** Show the prior label values in the "Already labeled" line. Megha can re-label by entering a new line for that session index; the most recent annotation per `session_id` wins.
7. **More than 10 sessions for the date.** Render the 10 most recent, print a tail message indicating how many were skipped, and require a re-run. (10 keeps the chat scannable; bump to 20+ if friction stays low.)
8. **Handle resolution failure.** If `requester_handle` or `addressee_handle` doesn't match anything in `household.md`, render the raw phone handle and proceed. Do NOT halt.
9. **Attribution on non-addressee phases.** `attribution_correct` only applies when an addressee message was sent. Persist `null` when no addressee_reach row exists.

## Error handling

- Kavi unreachable → halt with the message in step 2.
- Empty rows for date → exit cleanly (step 2).
- Unparseable reply → halt with the format help message in step 9. Already-parsed sessions in the same reply are NOT written (atomic per reply).
- Labels file write failure → halt with the OS error and `"no labels written this run."`

## What this command does NOT do

- Modify Kavi's runtime or its judgments file (read-only on the remote side).
- Compute aggregate metrics. Use the weekly aggregator (deferred — pattern follows `/eval-inbox-week`) for that.
- Push annotations back to Kavi. Annotations stay local; weekly ritual proposes spec / backlog updates based on accumulated labels.
- Rewrite or delete prior annotation rows. Re-labeling appends a new row; the most recent per `session_id` wins.
- Auto-detect "requester re-pinged" patterns. v1 trusts Megha's `cl` judgment at label time; an LLM-classified close-rate signal is queued as engineering work.

## Cross-references

- Schemas: `evals/definitions.md` → `eval-coordinates-judgments.jsonl` and `eval-coordinates-labels.jsonl`.
- Acceptance criteria + thresholds: `capabilities/kavi-coordinates.md` → Metrics.
- Daily eval ritual: `evals/ritual.md`.
- Companion eval surfaces: HTML viewer at `evals/viewer.html` loads `evals/inbox-to-task/traces/` (judgment side) and `evals/kavi-persona/traces/` (generative side).

## Mock walkthrough (for implementation reference; v1 is non-functional until the runtime endpoint lands)

This command is non-functional until the parallel runtime build exposes `GET /evals/kavi-coordinates/recent`. Until then, the procedure is exercised against a synthetic payload like:

```json
{
  "rows": [
    {
      "decision_id": "c_2026-05-06T03:41:55Z_a8b3f291",
      "session_id": "s_2026-05-06T03:41:55Z_rosa_cash",
      "ts": "2026-05-06T03:41:55Z",
      "phase": "ack",
      "requester_handle": "+15555550101",
      "addressee_handle": null,
      "inbound_text": "Hey Kavi, we need cash for Rosa tomorrow morning, can you check with Max if he already has it?",
      "ack_text": "Got it, checking with Max now. I'll let you know what he says.",
      "ack_latency_sec": 8
    },
    {
      "decision_id": "c_2026-05-06T03:42:18Z_b9c4e021",
      "session_id": "s_2026-05-06T03:41:55Z_rosa_cash",
      "ts": "2026-05-06T03:42:18Z",
      "phase": "addressee_reach",
      "requester_handle": "+15555550101",
      "addressee_handle": "+15555550102",
      "addressee_message_text": "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you have it on hand, or should one of you withdraw?",
      "addressee_message_attribution": true
    },
    {
      "decision_id": "c_2026-05-06T03:46:02Z_c0d5f132",
      "session_id": "s_2026-05-06T03:41:55Z_rosa_cash",
      "ts": "2026-05-06T03:46:02Z",
      "phase": "addressee_reply",
      "addressee_reply_text": "Yeah I have $100.",
      "addressee_reply_latency_sec": 224
    },
    {
      "decision_id": "c_2026-05-06T03:46:08Z_d1e6a243",
      "session_id": "s_2026-05-06T03:41:55Z_rosa_cash",
      "ts": "2026-05-06T03:46:08Z",
      "phase": "branch_decision",
      "branch": "4a_yes_have_it",
      "task_created": false,
      "task_id": null
    },
    {
      "decision_id": "c_2026-05-06T03:46:14Z_e2f7b354",
      "session_id": "s_2026-05-06T03:41:55Z_rosa_cash",
      "ts": "2026-05-06T03:46:14Z",
      "phase": "outcome_report",
      "outcome_report_text": "Max said he has it.",
      "outcome_report_latency_sec": 6,
      "closed": true
    }
  ]
}
```

After grouping by `session_id`, the rendered block is:

```
### Session 1 — `s_2026-05-06T03:41:55Z_rosa_cash`

**Requester (Megha → Kavi):** "Hey Kavi, we need cash for Rosa tomorrow morning, can you check with Max if he already has it?"

**Kavi → Megha (ack, 8s):** "Got it, checking with Max now. I'll let you know what he says."

**Kavi → Max (with attribution):** "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you have it on hand, or should one of you withdraw?"

**Max → Kavi (3 min 44s later):** "Yeah I have $100."

**Branch:** yes-have-it. **Task created:** no.

**Kavi → Megha (outcome, 6s):** "Max said he has it."

**Already labeled:** No.
```

A representative reply line:

```
1: cl=y fu=null at=y fp=n il=n "clean cash flow"
```

Produces this row appended to `eval-coordinates-labels.jsonl`:

```json
{"session_id": "s_2026-05-06T03:41:55Z_rosa_cash", "close_rate_pass": true, "follow_up_window_correct": null, "attribution_correct": true, "false_positive_task": null, "internal_ops_leakage": false, "notes": "clean cash flow", "annotated_at": "<UTC ISO now>", "annotator": "megha"}
```

(`false_positive_task` is `null` because `task_created == false` in the judgment rows; per schema, fp only applies when a task was created.)

Friction target: ≤ 60 seconds per call. The rendered view should fit in chat without scrolling for 5-10 sessions. Compact, scannable.
