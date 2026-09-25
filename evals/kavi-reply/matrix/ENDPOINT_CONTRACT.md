# POST /synthetic/verify/kavi-reply — endpoint contract

One-line description: the contract for the synthetic verify route that replays an inbound
family iMessage through the REAL intent parser and REAL reply composer of the intent-first
dispatch rebuild (PM-approved 2026-06-10), with executors in dry-run, and grades the result
against deterministic gates.

**Status:** authored 2026-06-10 by the matrix author, who is a different actor from the
implementation agent (BUILD_PIPELINE anti-Goodhart rule 1, 2026-06-02 incident
countermeasure). NOT frozen. The main engineer reviews this contract plus
`matrix-kavi-reply.jsonl`, then freezes via `python scripts/matrix_freeze.py freeze kavi-reply`.
The implementation agent builds the endpoint to satisfy this contract; the matrix author does
not implement.

**Incident this encodes:** 2026-06-10 evening (transcript: `evals/traces/exchanges.jsonl`
rows `x_2026-06-11T04:00:00Z` through `x_2026-06-11T04:06:48Z` on Kavi). "Yes. Also close
all tasks related to Anita's party and maple street camp" was bound to the wrong pending
question, coerced into the keep/drop-only vocabulary ("Kept: MJ Decide on Anita Rao
House Warming Party invite"), the maple intent was swallowed, and two minutes later Kavi
claimed "I don't have context on others from this thread."

---

## 1. Request body schema

All fields top-level in one JSON object. The first seven are the synthetic context the route
feeds to the real parser and composer. The last two are verifier-only expectation fields:
the route's gates read them; they are NEVER passed to the LLM.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `inbound_text` | string | yes | The verbatim inbound iMessage under test. |
| `sender` | `"megha"` \| `"max"` | yes | Logical sender. The route maps to the canonical handle from `household.md` for owner-prefix logic (`MJ ` = Megha, `MM ` = Max, unprefixed = Megha). |
| `recent_outbound` | array of `{kind: string, text: string}` | yes (may be `[]`) | Kavi's recent outbound messages to this sender, oldest first, LAST element = most recent. `kind` uses the runtime's outbound kinds (`periodic_summary`, `task_notification`, `post_action_reply`, `conversational`, `steering_ack`, `qa_ack`, ...). This is the offer-binding context. |
| `recent_inbound` | array of string | yes (may be `[]`) | The SENDER'S own prior inbound messages, oldest first, excluding `inbound_text`. This is the her-side context the old dispatcher never carried (the "I don't have context" failure class). |
| `pending_questions` | array of `{id: string, task_title_rendered: string, source_subject?: string}` | yes (may be `[]`) | Pending low-confidence Q&A questions, same shape as `state/questions.json` rows (minus timestamps; the parser binds by content and conversational adjacency, not wall-clock windows). |
| `pending_facts` | array of `{topic: string, text: string}` | yes (may be `[]`) | Facts awaiting confirmation (full text, not snippets, per the 2026-06-10 pending-facts change). |
| `open_tasks` | array of `{id: string, title: string}` | yes (may be `[]`) | The open MS To Do tasks visible to the executors. In dry-run this list IS the task store: target resolution happens against it and nothing else. |
| `open_coordination_sessions` | array of `{session_id: string, addressee: string, ask: string}` | no (default `[]`) | Open coordination sessions as parser context. No seed matrix case exercises this field yet; it is in the schema so coordination-aware cases can be added at the next re-freeze without a body-shape change. |
| `expected_intents` | array of `{type: string, target_keyword: string\|null}` | yes (may be `[]`) | Verifier-only. Each entry must be matched by at least one parsed intent (see matching rules, section 3). |
| `forbidden_intents` | array of `{type: string, target_keyword: string\|null}` | yes (may be `[]`) | Verifier-only. No parsed intent may match any entry. |

Unknown fields: reject with 400 (catches matrix/typo drift early rather than silently
ignoring an expectation field).

## 2. What the route does

1. **Build synthetic context** from the payload. No state files are read or written; the
   payload is the entire world. Sender allowlist, dedup, and tapback gates are NOT in this
   route (they are deterministic short-circuits upstream of the parser in production and are
   covered by unit tests, not matrix replay).
2. **Run the REAL intent parser** (one LLM call, the same skill + model as production)
   with the full context: `inbound_text`, `sender`, `recent_outbound`, `recent_inbound`,
   `pending_questions`, `pending_facts`, `open_tasks`, `open_coordination_sessions`.
   It returns the structured intent list (section 3).
3. **Run the deterministic executors in DRY-RUN mode.** Target resolution runs for real
   against the payload's `open_tasks` and `pending_questions`; every Graph/state mutation is
   simulated and recorded as a success result (`{intent, target_ids, simulated: true,
   result: "success"}`). No Graph calls, no state writes, no sends. Cross-owner and
   ambiguity rules execute exactly as in production (they are resolution logic, not
   mutation, so dry-run exercises them fully).
4. **Run the REAL reply composer** (LLM) with the parsed intents plus the simulated
   execution results, exactly as production would after real execution.
5. **Run the gate set** (section 4) over the parsed intents, the dry-run results, and the
   composed reply. Return the response (section 5).

## 3. Parsed-intent shape and matching rules

The parser's output contract (also the shape echoed back in the response):

```json
{"intents": [
  {"type": "close_task",
   "target_text": "all tasks related to Anita's party",
   "targets": [{"id": "t-anita-rsvp", "title": "MJ Decide on Anita Rao House Warming Party invite"}],
   "confidence": "high"}
]}
```

- `type` is one of the canonical vocabulary below. The vocabulary is a closed set for
  GATE-MATCHING purposes; the parser prompt itself describes intents in natural language
  and maps to these types (LLM vocabulary in, canonical types out).
- `targets` are resolved against the payload's `open_tasks` / `pending_questions` /
  `pending_facts`. A scoped instruction ("close all Anita tasks") may resolve to one
  intent with multiple targets or multiple intents with one target each; gates accept both.
- `target_text` is the verbatim span of the inbound (or the referenced outbound offer) the
  intent came from.

**Canonical intent types:**

| type | meaning |
|---|---|
| `qa_keep` | affirm a pending `[?]` question: task stays, `[?]` prefix removed |
| `qa_drop` | reject a pending `[?]` question: provisional task removed |
| `close_task` | mark an open task done |
| `create_task` | create a new task |
| `delete_task` | hard-delete a task (spec Steering: "Delete the X task") |
| `rename_task` | title edit |
| `undo` | reverse the most recent action |
| `pause` / `resume` | quiet-window steering |
| `correction` | learning-from-correction record (skip-going-forward etc.) |
| `coordination_reply` | inbound resolves an open coordination session |
| `clarify` | parser cannot resolve a target with confidence; compose ONE clarifying question, execute nothing for that span |
| `conversational` | chat, questions about Kavi's behavior, pending-fact delivery |

**Canonical-direction rule (binding for the implementation):** a close/done/handled
instruction whose target task also has a pending `[?]` question is `close_task`, not
`qa_drop` and never `qa_keep`. The executor resolves the pending question as a side effect
of the close (recorded as answered-by-close). This rule is what makes the incident case's
`forbidden qa_keep` and `expected close_task Anita` deterministic.

**Affirmative-resolution rule (binding):** a bare affirmative ("Yes", "Yea", "keep") is
never itself an intent type. The parser resolves it against the most recent outbound:
an offer to close X resolves to `close_task` on X; a `task_notification` Q&A resolves to
`qa_keep`/`qa_drop` on that question; a digest offering a pending fact resolves to
`conversational` (fact delivery). With no binding anchor, it is `clarify`.

**Expectation matching (used by gates 1 and 2):** an expectation entry
`{type, target_keyword}` matches a parsed intent iff `type` equals the intent's type AND
(`target_keyword` is null, OR the keyword appears case-insensitively as a substring of any
resolved target's `title`, or of the intent's `target_text`). Null keyword means "any
intent of this type."

## 4. Gate set

All gates run; any failure flips the verdict to FAIL. `failures[]` carries
`{gate, detail}` rows, same shape as the kavi-persona verify route.

| Gate | Fires when | Inputs |
|---|---|---|
| `intent_dropped` | An `expected_intents` entry matches NO parsed intent (section 3 matching). The tonight class: swallowed maple intent. | parsed intents (deterministic given them) |
| `wrong_direction_resolution` | A `forbidden_intents` entry matches ANY parsed intent. The tonight class: close coerced into keep; "Yes" bound to the wrong question. | parsed intents (deterministic given them) |
| `banned_template_reply` | Composed reply matches `^Got it\.$`, `^Got it!$`, `^Kept: `, or `^Dropped: ` (regex, anchored). The dead templates the rebuild kills. | reply text only (deterministic) |
| `unresolved_context_claim` | `recent_inbound` is non-empty AND the reply matches a no-context phrase pattern: "don't have context", "do not have context", "no context on", "don't have that context", "not sure what you're referring to". Canonical pattern list lives in the verify module (one home, extend there). Phrase heuristic; a paraphrased amnesia claim can escape it, documented limit. | reply text + payload (deterministic) |
| `ungrounded_action_claim` | The reply makes an AFFIRMATIVE past-tense/progressive action claim (verb list = the G-A1 canonical list in `kavi_runtime/structural_checks.py`) that no dry-run execution result backs. Must be negation-aware: "I haven't sent them" passes; "Sent them to Max" with no executed send fails. Claims backed by a simulated-success result pass (in dry-run the composer is allowed to narrate the simulated executions; that is the production path under test). | reply text + parsed intents + dry-run results (heuristic) |
| `length_cap` | Reply exceeds `structural_checks.LENGTH_CAP_TARGET` (reference the constant, never the literal, per the three-axes rule). | reply text (deterministic) |
| `prose_required` | Reply contains list markers (same check as the other verify routes). | reply text (deterministic) |

Deterministic vs parsed-intents-dependent: `banned_template_reply`,
`unresolved_context_claim`, `length_cap`, `prose_required` need only the payload and the
reply string. `intent_dropped` and `wrong_direction_resolution` are deterministic functions
of the parsed-intents JSON. `ungrounded_action_claim` needs parsed intents plus dry-run
results and is the one heuristic gate (negation handling); its pattern list gets one
canonical home in the verify module.

Gate logic home: `capabilities/kavi_persona/verify.py` (or a sibling module under
`capabilities/kavi_persona/`), imported by the runtime endpoint via
`kavi_runtime/synthetic_compose.py`, per the deep-verify-parity shape. The gates live in a
DIFFERENT artifact than the dispatcher code under iteration (anti-Goodhart rule, matrix in
`evals/` hashed, gates behind the endpoint).

## 5. Response shape

Same envelope as the other verify routes, plus the parsed intents and dry-run results
(Investigators replay on these; `run_matrix.py` reads only `output` / `verdict` /
`failures`):

```json
{
  "output": "<composed reply text>",
  "verdict": "PASS" | "FAIL",
  "failures": [{"gate": "intent_dropped|wrong_direction_resolution|banned_template_reply|unresolved_context_claim|ungrounded_action_claim|length_cap|prose_required", "detail": "..."}],
  "parsed_intents": [{"type": "...", "target_text": "...", "targets": [...], "confidence": "..."}],
  "executed": [{"intent_type": "...", "target_ids": ["..."], "simulated": true, "result": "success"}],
  "model": "...",
  "input_payload": {...}
}
```

## 6. Sibling compose route

Per the Phase 0c rule (CLAUDE.md: verify route MUST ship alongside a raw compose route),
`POST /synthetic/compose/kavi-reply` ships in the same change: same body schema minus the
two expectation fields (ignored if present), runs steps 1 to 4 only, returns
`{output, parsed_intents, executed, model, input_payload}` with no gates. It is the
Investigator's lower-level replay surface; the verify route is what Verifiers and
`run_matrix.py` call.

Both routes must be live on staging (port 8081, `staging_mode: true`) since matrix replay
targets staging by default.

---

## 7. Registry procedure draft: `deep:reply_intent`

The implementation agent adds the text below to `capabilities/_role_registry.md` under
"Procedure definitions" and extends the `kavi-persona` row's `verifier_procedure` cell to
`deep:periodic_summary + deep:reply_intent` (the capability now has two user-visible LLM
surfaces: the periodic summary composer and the inbound-reply path). The matrix author does
not edit the registry.

> ### `deep:reply_intent`
>
> Two endpoints, chosen by the bug shape (mirrors `deep:periodic_summary`):
>
> **Raw replay mode — `POST /synthetic/compose/kavi-reply`** (staging port 8081 by
> default; production port 8080 only for a deliberate Verifier run). Body carries a
> synthetic inbound exchange: `inbound_text`, `sender` ("megha"|"max"), `recent_outbound`
> [{kind, text}] (newest last), `recent_inbound` [string] (the sender's own prior
> messages), `pending_questions`, `pending_facts`, `open_tasks` [{id, title}], optional
> `open_coordination_sessions`. Runs the REAL intent parser with that context, the
> deterministic executors in DRY-RUN (resolution against the payload's `open_tasks` only,
> no Graph calls, no sends, no state writes), then the REAL reply composer over the
> simulated execution results. Returns `{output, parsed_intents, executed, model,
> input_payload}`. Use when the bug is about what the parser extracted or how the reply
> was phrased.
>
> **Gated verify mode — `POST /synthetic/verify/kavi-reply`.** Same body plus two
> verifier-only fields: `expected_intents` and `forbidden_intents`, each
> [{type, target_keyword}] matched against the parsed intents (type equality plus
> case-insensitive keyword-in-target-title-or-target-text; null keyword = any of that
> type). Returns `{output, verdict, failures[], parsed_intents, executed, model,
> input_payload}`.
>
> Gates (all run; any failing → verdict FAIL):
>
> - **intent_dropped** — an `expected_intents` entry absent from the parsed intents.
>   Catches the swallowed-intent class (2026-06-10: the maple street close that never
>   happened).
> - **wrong_direction_resolution** — a `forbidden_intents` entry present in the parsed
>   intents. Catches the wrong-binding and vocabulary-coercion classes (2026-06-10: "Yes"
>   bound to the Anita question instead of the Cedar House offer; "close" coerced to keep).
> - **banned_template_reply** — reply matches `^Got it\.$|^Got it!$|^Kept: |^Dropped: `.
>   The always-LLM rule's mechanical enforcement on this surface.
> - **unresolved_context_claim** — reply claims to lack context (canonical phrase list in
>   the verify module) while the payload's `recent_inbound` is non-empty. Catches the
>   "I don't have context on others from this thread" class.
> - **ungrounded_action_claim** — affirmative past-tense/progressive action claim
>   (G-A1 canonical verb list in `structural_checks.py`) with no matching dry-run
>   execution result; negation-aware.
> - **length_cap** — reply exceeds `structural_checks.LENGTH_CAP_TARGET`.
> - **prose_required** — reply contains list markers.
>
> Frozen seed matrix: `evals/kavi-reply/matrix/matrix-kavi-reply.jsonl` (18 cases,
> authored 2026-06-10 from the keep/drop-coercion incident transcript plus the
> kavi-persona.md Behavior examples). Per BUILD_PIPELINE Rule 5, a Verifier "fixed" claim
> on any inbound-reply bug requires the incident repro PASS AND this full matrix green.
