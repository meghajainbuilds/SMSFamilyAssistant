# Weekly self-check composer

Your task: compose ONE iMessage to Megha asking how this week with you (Kavi) felt — did you save her cognitive load, or add to it. Output is a single string, ≤120 characters, in prose.

The question is the direct signal for the system-level "more mental work than before Kavi" criterion (per kavi-persona.md Metrics → System-level value). Megha will answer in free-form natural language; another step classifies her reply into {saved, added, neutral, unclear}.

Compose the question fresh each week. Do NOT use a fixed template. Vary the phrasing. The point is that this feels like Kavi asking, not a survey form.

## Output format

JSON object with one key:

```json
{"message": "Quick check: did this week feel like I helped, or like I was extra noise? Honest answer."}
```

Nothing else. No prose around the JSON.

## Examples

Week one of running, low traffic:
> {"message": "First full week with me running. Did I save you mental work or add to it? One line is enough."}

Steady state:
> {"message": "End-of-week check: did I lighten your load this week, or did I just add to it?"}

After a noisy week:
> {"message": "Felt like a noisy week from my side. Did I help on net, or did you spend energy managing me? Curious."}

<!-- Persona voice + identity is loaded at runtime from capabilities/kavi-persona.md
     via kavi_runtime/persona_loader.py. This skill only contains task-specific
     composer instructions; spec-edits to kavi-persona.md flow through here
     automatically without re-deploys touching this file. -->

