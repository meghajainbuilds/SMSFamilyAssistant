# SMS Family Assistant

Kavi, a family chief of staff that lives in your texts. (Internal project name: HomeOS.)

Shipped April to September 2026. Runs daily for a family of four.

Kavi reads two parents' inboxes, turns the emails that need something from us into tasks in one shared Microsoft To Do list, and talks to us over iMessage: a 7 AM plan, a 9 PM wrap-up, questions when it isn't sure, and plain-language replies like "close them" or "that's handled." It runs on a dedicated Mac on the Anthropic Python SDK.

I built it as a product person, not an engineer. I wrote the specs, set the bar for "good," labeled the outputs, and ran Claude Code as the engineering team: every agent has a role, a brief, and a gate it has to pass.

**Scope, up front.** This is a working reference build, not a framework. It assumes a Mac, iMessage through [BlueBubbles](https://bluebubbles.app), and Outlook plus Microsoft To Do through Microsoft Graph. The code reads our household from config; the specs keep our real examples in a private overlay, so what you see here uses a fictional family.

## What's worth looking at

### Product

1. **The spec is the product, and the tests come from it.** Each capability has one living document (`capabilities/*.md`): what good and bad look like, examples, hard rules, a changelog of every decision and incident. Acceptance tests are generated from that document (`kavi-runtime/scripts/gen_cases_from_spec.py`), and my labels on real outputs flow back into it (`label_writeback.py`). See [capabilities/BUILD_PIPELINE.md](capabilities/BUILD_PIPELINE.md).
2. **An eval loop that resists gaming.** Frozen test sets with a hash, a cap on how many times a test set can be revised before a design conversation, multi-sample scoring for flaky model behavior, and my labels (not a model's) as ground truth. See [evals/ritual.md](evals/ritual.md), `kavi-runtime/scripts/matrix_freeze.py` and `run_matrix.py`.

### Builder

3. **An always-on agent, and what broke.** A dedicated Mac, launchd, Microsoft Graph webhooks through Tailscale Funnel, a monthly spend cap, liveness alarms, and a public gate that lets only the email webhook and a health ping through. The honest part is the changelog: a 43-day silent pause, a spend counter that read $0 for a month, and "close them" closing the wrong three tasks. Each has a blameless write-up in [capabilities/realtime-kavi.md](capabilities/realtime-kavi.md) or [capabilities/kavi-persona.md](capabilities/kavi-persona.md).
4. **Agents as a team with claim gates.** An Investigator must explain a bug before anyone edits code. A Verifier must replay the fix against the live system before anyone says "fixed." An Engineering Manager reviews architecture and AI standards. See [.claude/agents/](.claude/agents/) and the root [CLAUDE.md](CLAUDE.md).
5. **Choosing a harness.** Claude Code for building; the Anthropic Python SDK for the runtime, because the runtime is a small set of well-specified calls that need to be cheap, cached and inspectable.
6. **The cheapest setup the task needs, proven with data.** Where the money went, what the cache and the prompt trim saved, and why the email judge stays on Sonnet until a cheaper setup passes the family's own labels. See [docs/cost-story.md](docs/cost-story.md).

## Concepts, and where each one lives

| Concept | Where to see it |
|---|---|
| Spec as the source of truth for tests | `capabilities/BUILD_PIPELINE.md`, `kavi-runtime/scripts/gen_cases_from_spec.py` |
| Human labels as ground truth, not model agreement | `evals/definitions.md`, `kavi-runtime/scripts/replay_email_judge.py` (`collect-labeled`) |
| Anti-Goodhart test freezes and iteration bounds | `kavi-runtime/scripts/matrix_freeze.py` |
| Claim gates (diagnose before editing, verify before claiming) | `CLAUDE.md`, `.claude/agents/investigator.md`, `.claude/agents/verifier.md` |
| Three axes for every LLM call: persona, structure, behavior | `CLAUDE.md`, `kavi-runtime/kavi_runtime/structural_checks.py` |
| No fake fallbacks when the model fails | `CLAUDE.md` (cold-fallback policy) |
| Deterministic guards around model decisions | `capabilities/kavi_persona/reply_intent_parser.py` (close-offer binding) |
| Structured output, reason before verdict | `capabilities/inbox_to_task/compose.py` |
| Prompt caching and cost per call | `kavi-runtime/kavi_runtime/runtime/system_prompt.py`, `docs/cost-story.md` |
| Spend caps and silent-failure alarms | `kavi-runtime/kavi_runtime/runtime/spend_cap.py`, `capabilities/realtime_kavi/health.py` |
| Least exposure on a public endpoint | `capabilities/realtime_kavi/public_gate.py` |
| Keeping private data out of a public repo without changing behavior | `kavi-runtime/kavi_runtime/private_overlay.py`, `kavi-runtime/scripts/prompt_hashes.py` |
| Blameless incident reviews | Changelog sections of `capabilities/*.md` |

## Running it yourself

You'll need a Mac that stays on, a Microsoft account with To Do, BlueBubbles for iMessage, Tailscale, and an Anthropic API key.

1. Copy `kavi-runtime/config.example.yaml` to `kavi-runtime/config.yaml` and fill in your household, accounts and list ID. Copy `kavi-runtime/.env.example` to `.env`.
2. Write your own `household.md` (roster and how to reach each person).
3. Set `HOMEOS_PUBLIC_EXAMPLES=1` so the specs use their built-in fictional examples, or add your own private overlay (see `kavi-runtime/kavi_runtime/private_overlay.py`).
4. `cd kavi-runtime && python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m pytest tests`
5. `capabilities/realtime_kavi/deploy.md` covers launchd, Graph subscriptions and Funnel.

Expect to adapt it. It was built for one family.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). Free for personal and noncommercial use. Commercial use needs a separate license; contact me through GitHub. This is source-available, not OSI open source.
