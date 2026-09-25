# handlers.py complexity audit — 2026-05-06

`kavi-runtime/kavi_runtime/handlers.py` is 2919 lines, 1470 logical lines of
code. Single-file event-router for every webhook + every scheduled hook.
Per item #4 of the 2026-05-06 hygiene pass: identify the extraction
candidates, no actual extraction.

Tool: `radon cc -s -n B` for cyclomatic complexity, `awk` for line counts.

## Top 5 by cyclomatic complexity

1. **`email_arrived`** (line 649, CC = 75, grade F).
   The mainline email-to-task router. Loads the email, runs the inbox-to-task
   skill, dedupes, applies guardrails, emits the eval row, fires the iMessage
   notify. Candidate extraction module: **`email_pipeline.py`** (the per-email
   judgment + dedup + lifecycle + notify steps as separate functions, each
   pure-input/pure-output, with `email_arrived` as a thin orchestrator).

2. **`_try_handle_action_intent`** (line 1764, CC = 44, grade F).
   Stage 1 action-layer: classify, match, execute mark_done, compose ack.
   Today this is one function with deep branching across the three confidence
   tiers, the title-match outcomes, and the dry-run fallback. Candidate
   extraction module: **`action_layer.py`** (one function per action shape:
   `handle_mark_done`, `handle_create`, `handle_update`, `handle_cancel`).

3. **`imessage_received`** (line 1251, CC = 25, grade D).
   Top-level inbound iMessage router. Dispatches to correction classifier,
   action-intent, coordination, conversational reply, or pause-intent.
   Candidate extraction module: **`imessage_router.py`** (with a clean
   chain-of-responsibility: each classifier returns a verdict; the router
   routes on the first hit).

4. **`_handle_correction`** (line 2225, CC = 21, grade D).
   Correction classifier output → applies the correction (delete task, change
   owner, promote pattern, reject pattern). Many code paths because
   "correction" is a verb that means 6 different things. Candidate extraction
   module: **`corrections.py`** (one function per correction type;
   `_handle_correction` becomes a dispatch table).

5. **`_handle_create_task_verb`** (line 1585, CC = 20, grade C).
   The "tell Kavi to add this as a task" iMessage path. Title parsing, owner
   inference, dedupe, MS To Do write, ack composition. Candidate extraction
   module: **`task_verbs.py`** (the action-verb intents: `create_task`,
   `mark_done`, `update_title`, `change_owner` — all the imperative-iMessage
   verbs in one place).

## Top 5 by line count

These are the same shape as the cyclomatic top-5 (high CC and high LOC tend to
correlate); only `_send_imessage_with_fallback` and `_apply_lifecycle_update`
are line-heavy without being extreme on CC.

1. **`email_arrived`** — 550 lines. Same as #1 above.

2. **`_try_handle_action_intent`** — 354 lines. Same as #2 above.

3. **`_handle_correction`** — 160 lines. Same as #4 above.

4. **`_send_imessage_with_fallback`** (line 233, 154 lines, CC = 11).
   The canonical send wrapper. iMessage SEND attempt, BlueBubbles check,
   verification poll, fallback to Outlook email, outbound logging. Candidate
   extraction module: **`outbound_imessage.py`** (the verify-and-fallback
   logic separated from the event handlers — handlers just call
   `send_with_fallback(text, kind)` and don't see retry/verify plumbing).

5. **`_apply_lifecycle_update`** (line 500, 148 lines, CC = 15).
   Package lifecycle state machine: arrived → first_action → reply_update →
   final_action. Today inlined in handlers.py because it shares state with
   email_arrived. Candidate extraction module: **`package_lifecycle.py`**
   (already gestured at by `kavi_runtime/lifecycle.py` and
   `kavi_runtime/package_extractor.py`; the lifecycle state-transition code
   should consolidate there).

## Recommendation

Extraction is not urgent today — the file works. But every time someone
touches handlers.py, the CC=75 mainline `email_arrived` is the friction. The
right sequencing if Megha approves any extraction work:

1. Move `_apply_lifecycle_update` → `package_lifecycle.py` (low risk, the
   lifecycle module already exists).
2. Move `_send_imessage_with_fallback` → `outbound_imessage.py` (low risk,
   well-bounded, used by ~6 callers).
3. `_try_handle_action_intent` → `action_layer.py` (medium risk; shares state
   with the action-intent classifier eval logging).
4. `_handle_correction` → `corrections.py` (medium risk; the correction skill
   spec lives in inbox-to-task.md, so this is partly a code-org choice).
5. `email_arrived` last; biggest blast radius, highest payoff.

**No actual extraction in this audit pass.**
