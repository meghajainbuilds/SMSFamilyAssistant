"""Q&A loop helpers.

INTENT-FIRST REBUILD (2026-06-10): the legacy Q&A reply machinery was
DELETED, not bypassed —

- `_apply_qa_resolutions` (classify_qa_reply resolution applier)
- `_handle_qa_reply` (the "1 yes" legacy regex path)
- `_handle_q_and_a_reply` (the "Kept:" / "Dropped:" template acks)
- `_handle_examples_proposal_reply` (the "Got it! Added ..." template acks)

Their behavior classes now live in the intent parser + executors
(`capabilities/kavi_persona/reply_intent_parser.py`,
`capabilities/kavi_persona/intent_executors.py`) with the final reply
composed by ONE LLM call. Frozen-matrix coverage:
evals/kavi-reply/matrix/matrix-kavi-reply.jsonl cases
regression-bare-keep-after-qa, regression-bare-drop-after-qa,
mixed-keep-drop-close-one-message, incident-2026-06-10-yes-plus-close-all.

`_append_household_example` remains the canonical Examples-table writer,
used by the examples_proposal executor in intent_executors.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _append_household_example(household_path: Path, trigger: str, action: str, owner: str, signals: str) -> None:
    """Insert one row at the END of the Examples table in household.md.

    The Examples table sits between '### Examples table' and '### Adding an
    example'. We find the last row of the table (last line starting with '|'
    before that next section) and insert immediately after.
    """
    text = household_path.read_text()
    lines = text.split("\n")

    table_start_marker = "### Examples table"
    table_end_marker = "### Adding an example"
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if start_idx is None and line.strip() == table_start_marker:
            start_idx = i
        elif start_idx is not None and line.strip() == table_end_marker:
            end_idx = i
            break

    if start_idx is None or end_idx is None:
        raise RuntimeError("Examples table markers not found in household.md")

    # Walk back from end_idx to find the last table row (line starting with '|').
    last_row_idx = None
    for i in range(end_idx - 1, start_idx, -1):
        if lines[i].lstrip().startswith("|"):
            last_row_idx = i
            break

    if last_row_idx is None:
        raise RuntimeError("No table rows found in Examples table")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_row = f"| {trigger} | {action} | {owner} | {signals} Added {today} via correction promotion. |"
    lines.insert(last_row_idx + 1, new_row)
    household_path.write_text("\n".join(lines))


__all__ = [
    "_append_household_example",
]
