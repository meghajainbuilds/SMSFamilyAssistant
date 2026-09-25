# Household handles drift — 2026-05-06

## What's broken or at risk

`outbound_scanner.HOUSEHOLD_HANDLES` (Python constant, the deterministic gate) carries 3 identity surfaces that don't appear in `household.md` §iMessage handles. Today this is silent: the parity test passes because `household.md ⊆ runtime`. But the spec source of truth disagrees with what the runtime actually accepts/sends to. PM-visible question: which surfaces does the family treat as "Kavi can talk to me here?"

## Drift entries

| Handle | In household.md? | In runtime constant? | Notes |
| --- | --- | --- | --- |
| `megha.alt@example.com` | No (only in §Email identities for inbox scanning) | Yes | Megha's gmail — forwards into Outlook; not used as Apple ID |
| `max@example.com` | No (only in §Email identities) | Yes | Max's primary email |
| `max.alt@example.com` | No (only in §Email identities) | Yes | Max's email alias |

All 3 are email-form Apple IDs in the runtime allowlist. None appear in `household.md` §iMessage handles (which lists only Megha's `+15555550101` / `megha@example.com`, Max's `+15555550102`, and Kavi's `kavi@example.com`).

## Reconciliation options

**Option 1 — Add these 3 to `household.md` §iMessage handles.**
- User-visible impact: documents that Kavi may receive iMessage from these surfaces (e.g., if Max texts from his Outlook Apple ID instead of his phone). More identity surfaces tracked in spec.
- Cost: a household.md edit (gated content). Slightly fuzzier "is this person reachable here" picture for any future reader.

**Option 2 — Remove these 3 from `outbound_scanner.HOUSEHOLD_HANDLES`.**
- User-visible impact: if Max ever sends an iMessage from `max@example.com` (his Apple ID for iMessage), Kavi silently drops it at the inbound allowlist. Same for Megha's gmail-as-Apple-ID. Less iMessage routing coverage.
- Cost: tiny code edit. No spec change. But a real iMessage from Max could go missing without explanation.

## Recommendation

**Option 1.** Max's primary phone is `+15555550102`, but his email-form Apple IDs are real identity surfaces that Apple uses interchangeably for iMessage routing — dropping them risks silent missed messages from Max during travel or low-signal contexts. The `megha.alt@example.com` case is similar (Apple sometimes routes through it). Cost of adding 3 rows to `household.md` is near zero; cost of a missed Max iMessage during a real coordination is the whole point of building Kavi coordinates.

Megha to decide before next runtime change to `household.md` §iMessage handles.
