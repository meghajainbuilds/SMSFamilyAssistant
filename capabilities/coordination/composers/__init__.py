"""coordination composers — six LLM composer / classifier entry points
that own the coordination capability's outbound text and branch decisions.

Each function is DEFINED in its sibling module under
`capabilities/coordination/composers/`; this package surfaces them at the
package level so callers can do either:

    from capabilities.coordination.composers import compose_coordination_ack
    # or
    from capabilities.coordination.composers.ack import compose_coordination_ack

No re-export shims to `kavi_runtime/`. The function bodies live in the
sibling modules, not in `kavi_runtime/claude_client.py`. An Investigator
opening this directory finds the actual compose logic in one cd.
"""

from capabilities.coordination.composers.ack import (
    compose_coordination_ack,
)
from capabilities.coordination.composers.addressee_message import (
    compose_coordination_addressee_message,
)
from capabilities.coordination.composers.course_correction import (
    classify_coordination_course_correction,
)
from capabilities.coordination.composers.intent_classifier import (
    classify_coordination_intent,
)
from capabilities.coordination.composers.outcome import (
    compose_coordination_outcome,
)
from capabilities.coordination.composers.reply_parser import (
    parse_coordination_reply,
)

__all__ = [
    "compose_coordination_ack",
    "compose_coordination_addressee_message",
    "classify_coordination_course_correction",
    "classify_coordination_intent",
    "compose_coordination_outcome",
    "parse_coordination_reply",
]
