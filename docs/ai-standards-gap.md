# AI Engineering Standards Gap — HomeOS vs. the field (2025–2026)

**Purpose.** Give the Engineering Manager agent a sourced map of current AI-engineering
best practice from top voices (Anthropic, OpenAI, Hamel Husain, Eugene Yan, Shreya Shankar,
Simon Willison) and grade HomeOS against it: what's already strong, what's missing, what to
adopt next. Every external claim is cited with a URL. Written for a VP-of-Product reader:
decision-useful, no generic ML advice.

**Headline.** HomeOS is unusually mature on the parts most teams skip — frozen anti-Goodhart
matrices, claim gates, capability-doc-as-spec, structural separation of test from code. It is
behind the field on three things that are now table stakes: (1) a **validated** LLM-as-judge
(it has rubric dimensions but no human-alignment number), (2) a **frozen golden set actually
gating changes** (still L1, target L2), and (3) **production/online observability** beyond the
weekly hand-label. The shortlist at the end ranks the 5 gaps worth closing.

How to read each section: **Field recommends (cited)** → **HomeOS has** → **GAP / recommendation**.

---

## 1. Eval design: error analysis first, custom data-viewing tool, small samples

**Field recommends.**
- Error analysis on real traces is *the* highest-leverage eval activity, and it precedes
  metric choice: "Error analysis is **the most important activity in evals**" and generic
  prefab metrics "waste time and create false confidence." [Hamel Husain, *LLM Evals:
  Everything You Need to Know*](https://hamel.dev/blog/posts/evals-faq/)
- Start tiny: "Spend 30 minutes manually reviewing 20–50 LLM outputs whenever you make
  significant changes." [Hamel, evals-faq](https://hamel.dev/blog/posts/evals-faq/)
- Build a **custom annotation/data-viewing tool**; teams that do "iterate ~10x faster" — "the
  single most impactful investment you can make." [Hamel,
  evals-faq](https://hamel.dev/blog/posts/evals-faq/)
- "Your AI product needs evals" — the common root cause of failed LLM products is the absence
  of a robust eval system; the fix is domain-specific assertions + looking at data, not vendor
  metrics. [Hamel, *Your AI Product Needs Evals*](https://hamel.dev/blog/posts/evals/);
  [Simon Willison's summary](https://simonwillison.net/2024/Mar/31/your-ai-product-needs-evals/)

**HomeOS has.** This is a genuine strength. `evals/ritual.md` already encodes the L0–L4 ladder
explicitly "adapted from the maturity model Hamel and Eugene Yan describe," names L1 ("look at
your data") as "the highest-leverage step most teams skip," and HomeOS sits at L1 → target L2.
There is a purpose-built data-viewing tool (`evals/viewer.html`) with open-coding → axial-coding
in Sheets — exactly the custom-tool pattern Hamel calls a 10x investment. Sample sizes are
small-on-purpose (20-query matrices; 20–50 trace targets).

**GAP / recommendation.** Largely closed. The one weakness: error analysis is **weekly and
manual**, with no standing taxonomy of failure-mode codes carried forward run-to-run. The axial
codes live in ad-hoc Sheets, not in a versioned file the EM agent can read. **Recommendation:**
persist axial codes into a per-capability `failure-modes.md` (append-only) so the EM agent can
say "this regression is failure-class X, seen 4× before" instead of re-deriving each week. Low
effort; high compounding value.

---

## 2. LLM-as-judge: binary, few metrics, and *validated against humans*

**Field recommends.**
- **Binary pass/fail, not 1–5 Likert.** "A binary decision forces everyone to consider what
  truly matters." Multi-point scales: "people don't know what to do with a 3 or 4" and they
  "do not correlate" with what domain experts care about. [Hamel, *Using LLM-as-a-Judge*
  ](https://hamel.dev/blog/posts/llm-judge/)
- **Few metrics, one principal domain expert.** "If someone says you need to measure 8 things
  on a 1-5 scale, they don't know what they are looking for"; a single "principal domain expert"
  should set the standard. [Hamel, llm-judge](https://hamel.dev/blog/posts/llm-judge/)
- **Validate the judge against human labels** — track precision/recall (not raw agreement,
  "misleading when classes are imbalanced"), iterate to ">90% agreement." [Hamel,
  llm-judge](https://hamel.dev/blog/posts/llm-judge/)
- The judge inherits the model's flaws and needs its own human validation; **criteria drift** is
  real — "users need criteria to grade outputs, but grading outputs helps users define criteria."
  [Shankar et al., *Who Validates the Validators?* (UIST 2024)
  ](https://arxiv.org/abs/2404.12272)
- LLM judges are **systematically overconfident** relative to their accuracy — treat raw judge
  confidence with suspicion. [Eugene Yan, *Evaluating LLM-Evaluators*
  ](https://eugeneyan.com/writing/llm-evaluators/)
- Anthropic's own research-agent judge uses a **rubric of concrete dimensions** (factual
  accuracy, citation accuracy, completeness, source quality, tool efficiency) with a single LLM
  judge over ~20 representative queries. [Anthropic, *How we built our multi-agent research
  system*](https://www.anthropic.com/engineering/multi-agent-research-system)

**HomeOS has.** Strong alignment on shape: the Glean rubric (Completeness / Personalization /
Tone / Groundedness) is scored **binary 0/1**, not Likert (`evals/definitions.md`,
`capabilities/CLAUDE.md`) — exactly Hamel's prescription. The close-suggestion judge is logged
per-judgment (yes *and* no verdicts) so precision is measurable, and Goodhart counter-metrics
exist per metric (`evals/ritual.md`). Metric count is disciplined.

**GAP / recommendation.** The missing half is **judge–human alignment as a number**. HomeOS
grades outputs by rubric but never reports "the automated judge agrees with Megha's labels X% of
the time (precision/recall)" — so today the rubric scoring is effectively Megha hand-labeling,
not a validated auto-judge that can scale. Per Hamel this is the difference between L2 and L3.
**Recommendation:** before promoting any rubric dimension to an automated gate, run the judge
against Megha's existing labeled CSVs and report TPR/TNR; only ship the auto-judge on dimensions
that clear an agreement bar (Hamel's >90%). Also add a **criteria-drift note** to the weekly
ritual: when Megha's open codes introduce a new criterion, the rubric is stale and the judge must
be re-validated (Shankar). Medium effort; this is the single biggest eval-maturity unlock.

---

## 3. Multi-sample / variance: beating LLM noise

**Field recommends.** Evaluate over a **set of representative queries** and watch the *impact of
changes*, not single outputs — Anthropic started with "about 20 queries representing real usage
patterns" and notes single runs are noisy. [Anthropic, multi-agent research
system](https://www.anthropic.com/engineering/multi-agent-research-system) Calibration work
stresses that a single judgment is unreliable without repeated/uncertainty-aware sampling.
[Eugene Yan, *Evaluating LLM-Evaluators*](https://eugeneyan.com/writing/llm-evaluators/)

**HomeOS has.** This is **ahead of common practice**. The just-built Phase 1 graded scorecard
(`kavi-runtime/scripts/run_matrix.py`) runs each matrix case `--samples N` times and reports a
**pass-rate that climbs**, explicitly because "a single sample is a coin flip on borderline rows
(the bare-yea row flips ~50%)." It records a before/after delta per run in an append-only
`scorecard.jsonl` so "this edit moved the score +0.07" is a recorded fact, not a vibe. Most teams
still run single-sample matrices.

**GAP / recommendation.** Minor. The scorecard tracks mean pass-rate but not variance/CI per
case, so a high-variance case and a stable one look identical at the same mean. **Recommendation:**
log the per-case pass *count* spread (already have `pass_count`) and surface a simple "this case is
unstable (3/5)" flag. Low effort; turns the scorecard from a point estimate into a stability
signal. No action required to claim parity — this is gold-plating.

---

## 4. Agent engineering: tool design, orchestration, the subagent contract

**Field recommends.**
- **Start simple; add complexity only when it pays.** "Add complexity *only* when it demonstrably
  improves outcomes." [Anthropic, *Building Effective AI Agents*
  ](https://www.anthropic.com/engineering/building-effective-agents)
- **Invest in the agent-computer interface (ACI) as much as the human UI.** "Plan to invest just
  as much effort in creating good *agent*-computer interfaces (ACI)"; tools deserve "just as much
  prompt engineering attention as your overall prompts." **Test tools** on many example inputs and
  fix the failure modes (the SWE-bench absolute-filepath fix). [Anthropic, building effective
  agents](https://www.anthropic.com/engineering/building-effective-agents)
- **Five composable patterns:** prompt chaining, routing, parallelization, orchestrator-workers,
  evaluator-optimizer. [Anthropic, building effective
  agents](https://www.anthropic.com/engineering/building-effective-agents)
- **Subagent contract:** "Each subagent needs an objective, an output format, guidance on the
  tools and sources to use, and clear task boundaries." Multi-agent costs ~**15× more tokens** than
  a chat, so reserve it for high-value parallelizable work. [Anthropic, multi-agent research
  system](https://www.anthropic.com/engineering/multi-agent-research-system)
- **Evaluate end-state, not process** ("whether it achieved the correct final state" — agents find
  alternative valid paths); manual testing still finds edge cases evals miss. [Anthropic,
  multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
- **Risk-rate every tool** (read vs write, reversibility, financial impact) and gate high-risk
  actions with guardrails / human-in-the-loop. [OpenAI, *A Practical Guide to Building Agents*
  ](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/)

**HomeOS has.** Strong on most of this. The architecture is "simple composable patterns" (skills +
orchestrator loop, not a heavyweight framework). Investigator/Verifier are a clean
**orchestrator-worker + evaluator** split, and the Verifier is literally an end-state evaluator on
live Kavi. The `kavi-coordinates` capability is a routing/branching agent with a per-phase eval
schema. Action-claim grounding (G-A1) plus the `passes_g_a1` runtime gate is a real guardrail
against fabricated action claims. The cold-fallback policy is a thoughtful safety stance.

**GAP / recommendation.**
- **Tool-design eval is implicit, not measured.** HomeOS evals *composer output* heavily but has
  no explicit "did the agent call the right tool with the right args" tool-trajectory check of the
  kind Anthropic describes (the ACI investment). For `kavi-coordinates` (agentic) this matters.
  **Recommendation:** add a tool-call correctness check to the deep-verify route for agentic
  capabilities, not just output-shape + selection. Medium effort.
- **No explicit tool risk-rating ledger.** OpenAI's read/write/reversibility/financial rating isn't
  written down anywhere as a table, even though Kavi can send iMessages and write tasks (write +
  hard-to-reverse + reputational). The `staging_mode` hard-disable of outbound is a strong *de
  facto* guardrail, but the risk rationale lives in code, not a spec the EM agent can audit.
  **Recommendation:** a one-page tool-risk register (`capabilities/tool-risk.md`) rating each verb
  Kavi can fire. Low effort; high governance value for a system that messages real people.

---

## 5. Prompt engineering & optimization: spec as prompt, automated optimization

**Field recommends.**
- **Prompt engineering is the primary improvement lever** for agent behavior: "Since each agent is
  steered by a prompt, prompt engineering was our primary lever." [Anthropic, multi-agent research
  system](https://www.anthropic.com/engineering/multi-agent-research-system)
- **System prompts at the "right altitude"** — neither brittle hardcoded logic nor vague guidance;
  "the Goldilocks zone between two common failure modes." [Anthropic, *Effective Context
  Engineering*](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **Automated prompt optimization is now standard tooling** — DSPy/MIPROv2 (programmatic, compiles
  prompts from data), TextGrad (textual-gradient feedback), and vendor prompt improvers (Anthropic
  Console, OpenAI). [LangChain, *Exploring Prompt
  Optimization*](https://blog.langchain.com/exploring-prompt-optimization/); [DSPy
  overview](https://deepwiki.com/ombharatiya/ai-system-design-guide/7.2-dspy-and-prompt-optimization)

**HomeOS has.** The "altitude" discipline is *codified*, which is rare: the three-axis composer
rule (PERSONA / STRUCTURAL / BEHAVIOR each with one canonical home, no duplication;
`capabilities/CLAUDE.md`) is precisely the anti-brittle, anti-vague split Anthropic's altitude
advice gestures at, and the architectural test
`test_composer_skill_isolation.py` *enforces* it in CI. Capability-doc-as-spec means the spec and
the prompt are the same artifact (Hamel/Anthropic both endorse spec-driven behavior).

**GAP / recommendation.** Prompt iteration is **mostly manual**, but the substrate for closing
this is already being staked out: `evals/CLAUDE.md` (updated 2026-06-24) defines a **held-out
`improvement-set.jsonl`** (the climbing target, separate-by-design from the frozen
don't-regress matrix) and a Phase 3 `optimize_prompt.py` engine that scores the current skill,
proposes N variants, and ranks them. As of this writing the script + fixture are **specified but
not yet shipped** (the doc exists; the file doesn't). **Recommendation:** finish that engine as
the evaluator-optimizer loop — "propose prompt edit → score on the held-out set → keep if score
climbs and no counter-metric regresses" — and **gate it by the existing anti-Goodhart bound**
(10-run ceiling) so it can't overfit. The held-out/frozen split is the right call (matches
train/test hygiene); just make sure variant scores are re-measured on staging, not trusted as
predicted. Don't adopt DSPy wholesale (it wants a big labeled set HomeOS doesn't have at N=1);
borrow the *loop*, not the framework. Medium effort, high payoff, directly what Megha asked the EM
agent to do — and already half-designed.

---

## 6. Context management: finite resource, just-in-time, sub-agent isolation

**Field recommends.**
- **Context is finite with diminishing returns; "context rot" is real** — "as the number of tokens
  in the context window increases, the model's ability to accurately recall information from that
  context decreases." Aim for "the *smallest possible* set of high-signal tokens." [Anthropic,
  *Effective Context Engineering*
  ](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **Just-in-time retrieval** (load by reference at runtime) over pre-loading everything; **compaction**
  near the window limit; **sub-agent context isolation** (a subagent "returns only a condensed,
  distilled summary"). [Anthropic, *Effective Context
  Engineering*](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- "Most failures in production AI agents aren't model failures but context failures." [Anthropic
  context-engineering guidance, as summarized by
  howaiworks.ai](https://howaiworks.ai/blog/anthropic-context-engineering-for-agents) *(secondary
  source — paraphrase, treat as directional)*

**HomeOS has.** The whole repo is built on **just-in-time, reference-based context**: CLAUDE.md
files are explicitly "navigation only — read sibling files when you need their content," memory is
an index (`MEMORY.md`) of pointers, and `HOW_TO_FIX_A_BUG.md` is a 4-line router to the right axis
file. The Investigator/Verifier subagents are textbook context isolation (they explore widely and
return a condensed report). Megha's own feedback rules ("don't re-read plans defensively,"
"capabilities/CLAUDE.md is navigation, siblings own content") are context-engineering instincts.
This is **ahead of most teams**.

**GAP / recommendation.** Closed in spirit. No structural gap; the only watch-item is that
capability docs are growing long (kavi-persona, definitions.md), which is the "context rot" risk
applied to the *spec* the composer loads at compose time. **Recommendation:** periodically measure
the token weight of what `persona_loader.py` actually injects at compose time and keep it lean —
the same "smallest high-signal set" discipline, applied to the persona prompt. Low effort, monitor
only.

---

## 7. Safety, groundedness, hallucination

**Field recommends.**
- **Groundedness / faithfulness as a first-class metric** — faithfulness = proportion of claims in
  the output verifiable against the source; pair with citation accuracy. [Anthropic multi-agent
  rubric](https://www.anthropic.com/engineering/multi-agent-research-system); [RAGAS faithfulness,
  as summarized](https://www.confident-ai.com/blog/rag-evaluation-metrics-answer-relevancy-faithfulness-and-more)
- **Principles-based behavior specs** over rigid rules: models "exercise good judgment...by
  applying broad principles rather than mechanically following specific rules." [Anthropic,
  *Claude's Constitution* (Jan 2026)](https://www.anthropic.com/news/claude-new-constitution)
- **Deterministic guardrails for known threats** (blocklists, length limits, regex) layered under
  the model. [OpenAI, *Practical Guide to Building
  Agents*](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/)

**HomeOS has.** Groundedness is already a **named metric with its own formula** (`correct_grounded
/ (correct_grounded + ungrounded_reason)`) and its own annotation symbol — measuring "when Kavi
says 'I did X because Y,' is Y a real pattern in the spec?" That's a genuine
hallucination-of-reasoning guard, plus G-A1 guards hallucination-of-action. The capability docs are
**explicitly modeled on the Anthropic Model Spec** ("Model Spec ancestry, not a 2025 PRD"),
matching the principles-based direction. `structural_checks.py` is the deterministic guardrail
layer.

**GAP / recommendation.** Two small ones.
- The **Goodhart watch on Groundedness is acknowledged but spot-check-only** ("LLM citing safe
  Examples patterns regardless of fit, just to score grounded" → "spot-check 5 random rows/week").
  That's a known soft spot; fine for N=1 but the EM agent should keep an eye on it.
- No **citation-accuracy analog** for capabilities that will summarize external content
  (`url-summarizer`, `external-thread-monitor` are proposed). When those ship, faithfulness +
  citation accuracy should be in their rubric from day one (the eval-surface-ships-with-spec rule
  already forces this — just make groundedness explicit for retrieval types). Low effort, future-gated.

---

## 8. Observability & cost: online/production evals, tracing, token accounting

**Field recommends.**
- **2025 baseline = distributed tracing + token accounting + automated evals + human feedback
  loops**; prompt-completion linkage and multi-agent traceability are the common gaps. [Maxim,
  *LLM Observability Best Practices for
  2025*](https://www.getmaxim.ai/articles/llm-observability-best-practices-for-2025/) *(vendor
  source — directional)*
- **Online/production evals on real traffic** (L4) sit above offline labeled sets; standardize
  offline runs *and* automated log evaluation + CI/CD gates. [Maxim RAG eval
  guide](https://www.getmaxim.ai/articles/rag-evaluation-a-complete-guide-for-2025/) *(vendor)*
- **Token cost is the dominant variable** in agent systems (15× chat; "token usage alone
  explain[s] 80% of performance variance"). [Anthropic, multi-agent research
  system](https://www.anthropic.com/engineering/multi-agent-research-system)

**HomeOS has.** Token accounting is **already in the trace schema** (every `usage` block logs
input/output/cache tokens per decision), there are latency + spend-cap + webhook-flood regression
alerts (`evals/definitions.md`), and a hard spend-cap pause. Cost reasoning is mandated in the
role contract (compute dollars, don't say "high"). This is more cost-disciplined than most hobby
or even production builds.

**GAP / recommendation.** The real gap. HomeOS observability is **offline + weekly** — there is no
**online/production eval loop** running automated checks on live traffic and surfacing drift in
near-real-time. Today a regression is caught Friday at the earliest (or when Megha forwards an
email back from her inbox). The frozen matrix runs only when an engineer invokes it; nothing
*continuously* samples production behavior. **Recommendation:** a lightweight **production canary** —
sample N real decisions/day, run them through the deep-verify route automatically, and alert on a
pass-rate drop. This is the L3→L4 step the ritual already names as the target. Medium effort; this
is what turns "evals" into "continuous evals," which is Megha's stated standing requirement.

---

## Where HomeOS is already ahead of common practice

State these plainly — they are real differentiators the EM agent should *preserve*, not re-derive:

1. **Anti-Goodhart frozen matrices** (`BUILD_PIPELINE.md`): the test is hashed and frozen *before*
   fix iteration, owned by a different artifact than the code under iteration, with a bounded
   iteration ceiling (10 runs) that exits nonzero even when green. This directly counters the
   reward-hacking failure mode (the 2026-06-02 incident where agents edited caps to make tests
   green). Most teams have nothing like this. The field talks about Goodhart; HomeOS *mechanized*
   the countermeasure.
2. **Claim gates** (Investigator before diagnosis, Verifier before "fixed"): a structural
   separation between "the test passed" and "the user-visible behavior works on the live device,"
   enforced by sub-agents. This is stronger than the "manual testing still matters" caveat in
   Anthropic's own writeup — HomeOS made it a gate, not a reminder.
3. **Capability-doc-as-spec with mechanical conformance** (Rule 3: "a spec rule without a matrix
   case doesn't exist"): the spec, the prompt, and the eval rubric are the same artifact, and a
   new rule can't land without a test in the same commit. This is the spec-driven ideal most teams
   aspire to and few enforce.
4. **Three-axis composer isolation** enforced in CI: PERSONA / STRUCTURAL / BEHAVIOR each have one
   canonical home and a test that fails if they leak into each other.
5. **Graded multi-sample scorecard** with recorded before/after deltas — variance-aware scoring
   that most single-sample matrices lack.

---

## Prioritized shortlist — top 5 gaps to close

Ranked by leverage for the EM agent's mandate (enforce standards + suggest what's missing).

| # | Gap | Effort | Why it matters |
|---|-----|--------|----------------|
| **1** | **Validate the LLM-as-judge against Megha's labels** — report judge↔human precision/recall per rubric dimension; only auto-gate dimensions clearing ~90% agreement. Add a criteria-drift check to the weekly ritual. | Medium | This is the L2→L3 unlock. Today rubric "scores" are really Megha hand-labeling; without an alignment number, an automated judge can't be trusted to scale. [Hamel](https://hamel.dev/blog/posts/llm-judge/), [Shankar](https://arxiv.org/abs/2404.12272) |
| **2** | **Production canary / online eval loop** — auto-sample N live decisions/day through the deep-verify route, alert on pass-rate drop. | Medium | Closes the L3→L4 gap the ritual already names. Turns "evals" into the *continuous* evals Megha requires; cuts regression-detection latency from a week to a day. [Maxim 2025](https://www.getmaxim.ai/articles/llm-observability-best-practices-for-2025/) |
| **3** | **Finish the evaluator-optimizer prompt loop** — `optimize_prompt.py` + per-capability `improvement-set.jsonl` are *specified* in `evals/CLAUDE.md` (2026-06-24) but the script/fixtures aren't shipped yet. Complete it: propose prompt edit → score on held-out set → keep if score climbs and no counter-metric regresses, capped by the 10-run anti-Goodhart bound, variants re-measured on staging. | Medium | Exactly the EM agent's job, and already half-designed (held-out vs frozen split is correct train/test hygiene). Frozen matrix + graded scorecard provide the scored objective; the anti-Goodhart bound makes it safe. [Anthropic evaluator-optimizer](https://www.anthropic.com/engineering/building-effective-agents) |
| **4** | **Tool-risk register + tool-trajectory eval** — a one-page table rating each verb Kavi can fire (read/write, reversibility, financial/reputational), plus a tool-call-correctness check in deep-verify for agentic capabilities. | Low–Medium | Kavi messages real people and writes shared tasks (write + hard-to-reverse). The risk rationale currently lives in code, not an auditable spec. [OpenAI agents guide](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/), [Anthropic ACI](https://www.anthropic.com/engineering/building-effective-agents) |
| **5** | **Persisted failure-mode taxonomy** — append axial codes from weekly open-coding into a per-capability `failure-modes.md` the EM agent can read. | Low | Lets the EM agent recognize recurring failure classes ("seen 4× before") instead of re-deriving each week; compounds the error-analysis investment Hamel calls highest-ROI. [Hamel evals-faq](https://hamel.dev/blog/posts/evals-faq/) |

**Honorable mentions (monitor, don't build yet):** per-case variance/CI on the scorecard (gold-plating);
groundedness Goodhart is spot-check-only (fine at N=1, watch it); citation-accuracy rubric for the
proposed retrieval capabilities (future-gated, the eval-ships-with-spec rule already forces it).

---

## Claims I could NOT fully source (flagged)

- **"Most failures in production AI agents aren't model failures but context failures"** and
  **"context engineering is the #1 job of engineers building AI agents"** — these appear in
  *secondary* summaries of Anthropic's context-engineering post
  ([howaiworks.ai](https://howaiworks.ai/blog/anthropic-context-engineering-for-agents)), not
  verified verbatim in the primary Anthropic article I fetched. The primary article *does* support
  the underlying point ("context must be treated as a finite resource"; "smallest possible set of
  high-signal tokens"). Treat the punchy quotes as directional paraphrase, not Anthropic's exact words.
- **LLM-observability "2025 baseline" claims** (distributed tracing + token accounting + automated
  evals as table stakes) come from a **vendor blog** (Maxim), not a neutral primary source. The
  direction is consistent with practitioner consensus but the specific framing is vendor marketing —
  cite as directional.
- **DSPy accuracy figure (46.2% → 64.0%)** came from a search-snippet study summary I did not open
  to the primary paper. Cited the LangChain/DSPy overview pages for the *existence and shape* of
  automated prompt optimization, which are solid; the specific percentage is unverified.
- **Eugene Yan / Chip Huyen "GTC 2025" collaboration** surfaced only in search snippets; I did not
  verify a primary artifact. Not load-bearing for any recommendation — dropped from the body.

_Last updated 2026-06-24. Author: research pass for the Engineering Manager agent. All primary
Anthropic/OpenAI/Hamel/Shankar/Yan claims verified against the cited URL; secondary/vendor claims
flagged above._
