"""Side-effect-free replay entry points for the HomeOS Verifier sub-agent.

Phase 4 (2026-06-02): physical move. All replay / verify function bodies
live in `capabilities/<name>/verify*.py`. This module is a thin re-export
so existing callers (`kavi_runtime/server.py`, scripts) keep working.
"""

from __future__ import annotations

# kavi-persona periodic_summary verify (capabilities/kavi_persona/verify.py).
from capabilities.kavi_persona.verify import (  # noqa: F401
    replay_periodic_summary,
    verify_periodic_summary_selection,
    _title_keywords,
    _output_names_title,
    _detect_duplicate_phrases,
)

# kavi-persona inbound-reply verify (intent-first rebuild 2026-06-10;
# capabilities/kavi_persona/verify_reply.py).
from capabilities.kavi_persona.verify_reply import (  # noqa: F401
    replay_kavi_reply,
    verify_kavi_reply,
    validate_reply_payload,
)

# inbox-to-task verify (capabilities/inbox_to_task/verify.py).
from capabilities.inbox_to_task.verify import (  # noqa: F401
    replay_email_classify,
    verify_email_classify_selection,
)

# kavi-coordinates verify (capabilities/coordination/verify.py).
from capabilities.coordination.verify import (  # noqa: F401
    replay_coordination_addressee,
    verify_coordination_selection,
)

__all__ = [
    "replay_periodic_summary",
    "verify_periodic_summary_selection",
    "replay_kavi_reply",
    "verify_kavi_reply",
    "validate_reply_payload",
    "replay_email_classify",
    "verify_email_classify_selection",
    "replay_coordination_addressee",
    "verify_coordination_selection",
]
