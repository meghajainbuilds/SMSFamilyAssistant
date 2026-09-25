# What Kavi costs to run, and how that changed

Raw material for a post. Numbers are **measured** unless marked *estimate*. Sources are listed at the bottom. The last section gets filled in once the fixes have been live for a few weeks.

## The one-line version

In May, an audit predicted that Kavi would cost about $100 a month and hit its $30 cap around the 9th of every month. We shipped half of the fix. In August the cap tripped on the 9th, exactly as predicted. A second bug then kept Kavi silently paused for 43 days. The skipped half of the fix is also the half that cuts the bill by about 80%.

## Timeline

| When | What happened | Monthly cost |
|---|---|---|
| May 5 | Kavi goes live. Every call uses one model (Sonnet 4.6). The cap is set at $30, "2x the expected $15." | Not measured |
| May 6 | A cost audit finds most spend goes to marketing email that Kavi then skips. It predicts **$101 a month, 3.4x the cap**, with a pause around day 9. Its recommendations: a newsletter filter in code, a 1-hour prompt cache, and a trimmed prompt. Estimated savings: $70 to $95 a month. | *Estimate: $101* |
| May 6 | **Shipped:** the 1-hour cache and cheaper Haiku for two small classifiers. **Parked:** the newsletter filter, which only runs in "log what I would skip" mode; it never became real. **Not done:** the prompt trim. | |
| June 10 | We learn the spend counter had read $0.00 since day one. The $30 cap had never been able to trip. The counter gets rebuilt. | June: $61 (counting from June 10) |
| July | First full month with a working counter. | **$77** |
| Aug 9 | The cap trips. The audit had predicted this day in May. | Aug: $38 (paused on the 9th) |
| Aug 11 | I text "resume." A bug re-pauses Kavi one second later, having processed 0 of 92 emails. Nothing alerts anyone. The daily summaries keep arriving, so it looks like a quiet inbox. | |
| Sep 23 | Found during a routine check-in. 3,100 emails have queued over 43 days. | Sep: $7.92 so far (summaries only) |

## Where the money actually went (June 1 to Aug 9)

The data comes from Kavi's own per-call logs, which match the spend counter to within 1%.

| Job | What it does | Share of spend |
|---|---|---|
| Email judge | Reads each email and decides "task or skip" | **72%** |
| Morning and evening summaries | Kavi's 7 AM and 9 PM messages | 10% |
| Duplicate check | "Is this new task a repeat?" | 9% |
| Everything else | Replies, questions, corrections | 9% |

What stood out:

- **87% of email judgments end in "skip."** We pay the most for the least valuable email: LinkedIn, Substack, retail newsletters.
- **$0.057 per email and $0.48 per task actually created.**
- **Each email judgment sends a prompt of about 40,000 tokens.** The judge loads my entire spec for the email capability, changelog included, plus Kavi's persona and voice rules. All of that to return one line of JSON.
- **A quarter of emails get judged twice.** Microsoft sends each email notification about twice, and both copies often slip past the duplicate guard. *Estimate: about $16 a month wasted.* Root cause being confirmed.
- **The counter still under-counted.** It priced the 1-hour cache at the 5-minute rate. *Estimate: real spend is about $11 a month higher than recorded.*

## What I'm changing (September)

The principle: **each task gets the cheapest model that passes its eval.** "Cheap" alone isn't the rule; the eval is what makes cheap safe.

| Change | Quality risk | *Estimated* savings per month |
|---|---|---|
| Trim the email judge's prompt to the rules it needs | None expected; the test suite confirms | ~$30 |
| Stop judging the same email twice | None | ~$16 |
| Stop loading the email spec into unrelated jobs | None | ~$6 |
| Duplicate check moves to Haiku | Low | ~$5 |
| Email judge moves to Haiku, **only if** it agrees with Sonnet on at least 95% of past task decisions and I review the disagreements | Medium, gated | ~$10 |
| Newsletter filter goes live with an exceptions list I approve | Medium, gated | ~$5 |
| Kavi's voice (summaries, replies) stays on the stronger model | Deliberate | $0 |

*Projected:* about $77 to $88 a month drops to about **$15 to $20**. The $30 cap goes back to being a 2x safety margin, not a monthly tripwire.

## Lessons (draft angles for the post)

1. **The audit was right, and shipping half of it was the mistake.** We shipped the easy half, caching and two small model swaps. We parked the half that needed judgment: which email is safe to skip without asking the model. The parked half was where the money was.
2. **A guardrail you can't observe is decoration.** For five weeks the cap read $0 and could never trip. Later it tripped and then stuck, silently, for 43 days. Each safety feature needs its own "am I working?" signal.
3. **Backstops get scoped to the last incident.** After a June outage, we added a stuck-pause alarm for "quiet mode" only. The August pause was a spend pause, so no alarm fired.
4. **Prompts grow like closets.** Each spec edit made the judge's prompt a little bigger, and nobody asked what the judge actually needed to read. The cost was invisible until someone divided dollars by tasks.
5. **Measure cost per outcome, not per call.** "$0.02 a call" sounds cheap. "$0.48 per task created, mostly spent saying no to newsletters" tells you what to fix.
6. **The cheapest model is an eval decision, not a pricing decision.** Haiku costs a third of Sonnet. Whether it's allowed to judge my email depends on whether it agrees with Sonnet on the emails that matter.

## Now (fill in after 2 to 4 weeks live)

- Measured monthly spend:
- Cost per email / per task created:
- Share of email handled by the free filter:
- Haiku vs Sonnet agreement on task decisions:
- Anything the cheaper setup got wrong:

## Sources

- `kavi-runtime/audits/token_optimization_2026-05-06.md`: the May 6 prediction ($101 a month, pause around day 9, $70 to $95 in savings).
- `kavi-runtime/audits/model_audit_2026-05-06.md`: the per-call model inventory.
- `capabilities/realtime-kavi.md` changelog: the 2026-06-10 counter rebuild, the 2026-07-14 quiet-pause fix and the 2026-09-23 resume fix.
- Kavi's spend counter (`state/anthropic_spend.json`): monthly totals for June to September.
- Kavi's per-call traces (`evals/traces/exchanges.jsonl`): the job breakdown, cost per email and duplicate rate. Profiled 2026-09-23.
