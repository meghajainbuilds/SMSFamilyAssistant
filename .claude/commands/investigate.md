---
description: Force-fire the Investigator sub-agent on a bug symptom. Use when Megha suspects the engineer is about to make a diagnosis claim without proper root-cause analysis, or when she wants to investigate a symptom independently.
allowed-tools:
  - Agent
argument-hint: "<symptom statement>"
---

# /investigate — force-fire the Investigator

Megha invokes this when she wants the Investigator sub-agent to produce a root-cause report on a symptom, regardless of whether the main engineer is about to make a diagnosis claim.

## Procedure

1. **Capture the symptom.** Take the user's argument verbatim. If no argument provided, ask: "What symptom should I investigate?" and stop.

2. **Spawn the Investigator sub-agent.** Use the Agent tool with `subagent_type: investigator` and the symptom as the prompt. The Investigator's procedure is defined in `.claude/agents/investigator.md`.

3. **Return the report verbatim.** Don't summarize, don't add context, don't soften. Paste the Investigator's full structured report.

4. **Stand by.** Wait for Megha's ACK before any code edit (per the standard block protocol in `CLAUDE.md`).

## What this command does NOT do

- Edit code, deploy, or modify Kavi state. The Investigator is read-only by design.
- Decide the fix. That's a conversation between Megha and the main engineer after the report.
- Run the Verifier. Use `/verify` for that, after a fix is applied.
