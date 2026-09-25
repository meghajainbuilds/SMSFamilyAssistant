"""kavi-persona composers — every Megha-facing LLM call lives here.

Phase 4 (2026-06-02) physical move: `compose_periodic_summary` body now
lives at `composers/periodic_summary.py`. Remaining composers
(`compose_qa_question`, `compose_post_action_reply`,
`compose_action_clarifying_reply`, `compose_weekly_self_check`,
`compose_kavi_conversation`, `compose_correction_classifier`,
`compose_batch_action_reply`, `match_target_to_open_task`,
`resolve_pending_action_clarification`, etc.) still live as methods on
`kavi_runtime/claude_client.ClaudeClient` pending follow-on Phase 4 work.

Use either:
    from capabilities.kavi_persona.composers import compose_periodic_summary
    # or
    from capabilities.kavi_persona.composers.periodic_summary import (
        compose_periodic_summary,
    )

For unmigrated composers, call via the `ClaudeClient` proxy:
    from kavi_runtime.claude_client import ClaudeClient
    client = ClaudeClient(config)
    text = client.compose_qa_question(...)
"""

from capabilities.kavi_persona.composers.periodic_summary import (
    compose_periodic_summary,
)

__all__ = [
    "compose_periodic_summary",
]
