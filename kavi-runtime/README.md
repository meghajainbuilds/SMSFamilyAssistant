# kavi-runtime

The always-on Python service that runs on Kavi's Mac. Replaces v0/v0.1 Claude Code slash-command flow with an event-driven pipeline:

- MS Graph webhook fires when a new email arrives
- Anthropic API (Sonnet 4.6) judges the email via the email-to-tasks prompt
- High-confidence tasks land in MS To Do automatically
- Low-confidence or priority-sender emails trigger an iMessage to Megha via BlueBubbles
- BlueBubbles webhook receives Megha's replies; runtime updates MS To Do and confirms back
- Skip-corrections from Megha are logged and applied on next email; promoted to `capabilities/inbox-to-task.md` Examples after 2+ same-pattern corrections

Spec: `../plans/2026-04-28-realtime-kavi-v0.2.md`

## Layout

```
kavi-runtime/
├── pyproject.toml            # package metadata + deps
├── config.yaml               # runtime config (paths, ports, schedules)
├── .env.example              # shape of env vars; real .env at ~/.config/kavi/.env
├── kavi_runtime/
│   ├── __init__.py
│   ├── main.py               # entrypoint; wires server + scheduler
│   ├── server.py             # FastAPI webhook receiver (MS Graph + BlueBubbles)
│   ├── handlers.py           # event handlers (email_arrived, imessage_received)
│   ├── scheduler.py          # cron jobs (periodic summary, correction-pattern detection)
│   ├── claude_client.py      # Anthropic SDK wrapper with prompt caching
│   ├── graph_client.py       # MS Graph SDK wrapper (mail + tasks + subscriptions)
│   ├── bluebubbles_client.py # BlueBubbles HTTP API wrapper
│   └── state.py              # JSONL appenders (corrections, runs); state file readers
├── skills/
│   ├── email_to_tasks.md          # ported from .claude/skills/email-to-tasks/SKILL.md
│   ├── task_writer_mstodo.md      # ported from .claude/skills/task-writer-mstodo/SKILL.md
│   └── correction_classifier.md   # new for v0.2: parse free-text iMessage corrections
└── launchd/
    └── com.megha.kavi.plist  # launchd service for auto-start on boot
```

## Status

Skeleton only as of 2026-04-28. Build sequence in the v0.2 plan; modules contain TODO markers that map to the build steps.

## Running locally (dev, on Kavi's Mac)

```
uv venv
uv pip install -e .
uv run kavi-runtime
```

## Running as a service (production, on Kavi's Mac)

```
cp launchd/com.megha.kavi.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.megha.kavi.plist
launchctl start com.megha.kavi
```

Logs: `~/Library/Logs/kavi-runtime.log`
