---
description: Force-fire the Verifier sub-agent on a recent Investigation report. Use when Megha suspects a "fixed" claim arrived without Verifier evidence, or when she wants to re-verify a previously-passed fix.
allowed-tools:
  - Agent
argument-hint: "<investigation report path | last>"
---

# /verify — force-fire the Verifier

Megha invokes this when she wants the Verifier sub-agent to re-run an Investigation report's PASS criterion against live Kavi, regardless of whether the main engineer has spawned it.

## Procedure

1. **Capture the reference.** Take the user's argument verbatim. Accept either a path to an Investigation report or the literal string `last`. If no argument, default to `last`.

2. **Spawn the Verifier sub-agent.** Use the Agent tool with `subagent_type: verifier` and the reference as the prompt. The Verifier's procedure is defined in `.claude/agents/verifier.md`.

3. **Return the verdict verbatim.** Don't summarize, don't soften a FAIL to "almost passed." Paste the Verifier's full structured verdict.

4. **Stand by.** If FAIL, wait for Megha's direction. If PASS, surface the evidence so Megha can confirm.

## What this command does NOT do

- Edit code, deploy, or modify Kavi state. The Verifier is read-only by design.
- Claim "fixed" on the engineer's behalf. The verdict is evidence; the engineer reports status using it.
- Re-investigate. If verification reveals new evidence the original Investigation report didn't cover, the engineer spawns the Investigator again.
