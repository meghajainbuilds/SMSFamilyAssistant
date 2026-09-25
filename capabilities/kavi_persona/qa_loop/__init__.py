"""kavi-persona iMessage Q&A loop.

The Q&A loop is how Kavi resolves uncertainty: when a low-confidence task
or examples-promotion proposal needs Megha's confirmation, Kavi queues a
pending question (`questions.json`), composes the question via the Q&A
composer, sends the iMessage, and waits for the reply.

Reply handling (intent-first rebuild 2026-06-10): the LLM intent parser
(`capabilities/kavi_persona/reply_intent_parser.py`) extracts qa_keep /
qa_drop intents; the executors in
`capabilities/kavi_persona/intent_executors.py` resolve the question
(task-confirmation Q&A or examples-promotion proposal, dispatched by the
question's `kind`), including answered-by-close when a close_task covers
the question's task. The legacy reply handlers and their template acks
were deleted in that rebuild; `handler.py` keeps the Examples-table
writer (`_append_household_example`).

Title editors physically moved to `capabilities/kavi_persona/actions/title_edits.py`
in Phase 4 (2026-06-02).

State: `/Users/kavi/HomeOS/state/questions.json` (per-concept Phase 3 file).
"""

from kavi_runtime.state_per_concept import (  # noqa: F401
    load_questions,
    save_questions,
)

__all__ = [
    "load_questions",
    "save_questions",
]
