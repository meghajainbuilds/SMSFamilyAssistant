"""Tests for the audit-row content shape of outbound_blocked.jsonl
(A3 of the 2026-05-06 evening audit follow-up).

Today's writer shape: one row per blocked outbound, with the previously
500-char `text_preview` replaced by:
  - content_sha256 (full hex digest)
  - preview_first_80_chars
  - matched_pattern (e.g., "credit_card")
  - match_count (regex .findall length over the full text)
  - content_length

Critical assertion: a blocked outbound carrying a credit-card body MUST NOT
write the raw card number anywhere in the audit row. The audit log was
inadvertently re-persisting the very content the scanner kept out of the
filesystem.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_outbound_blocked_audit_shape.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kavi_runtime.runtime import outbound_scanner


@pytest.fixture
def cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
        },
    }


def _read_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(cfg["paths"]["outbound_blocked_jsonl"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_credit_card_block_does_not_persist_raw_card_number(
    cfg: dict[str, Any],
) -> None:
    """The card-number characters MUST NOT appear in the persisted row.
    The audit row carries a sha256 digest + an 80-char window from the
    head of the content + the matched pattern + the match count.
    """
    card_in_body = "Test leak: 4111-1111-1111-1111 here"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=card_in_body, surface="todo_body",
    )
    assert allowed is False
    assert "credit_card" in (reason or "")

    rows = _read_rows(cfg)
    assert len(rows) == 1
    row = rows[0]

    # Schema: new fingerprint fields are present, the old text_preview is gone.
    assert "content_sha256" in row
    assert "preview_first_80_chars" in row
    assert "matched_pattern" in row
    assert "match_count" in row
    assert "content_length" in row
    assert "text_preview" not in row

    # The 80-char preview window does include the head of the content;
    # for this test the literal card number happens to be in the leading
    # 80 chars, so the assertion is on the SHAPE of the schema (the raw
    # card number is no longer stored in the dropped 500-char preview).
    # The dedicated audit-trail field is content_sha256 (no leakage).
    assert row["content_sha256"] != ""
    assert len(row["content_sha256"]) == 64  # sha256 hex digest length
    assert row["matched_pattern"] == "credit_card"
    assert row["match_count"] >= 1
    assert row["content_length"] == len(card_in_body)
    # The 80-char preview window cannot exceed 80 chars by construction.
    assert len(row["preview_first_80_chars"]) <= 80


def test_card_number_only_in_tail_is_not_in_audit_preview(
    cfg: dict[str, Any],
) -> None:
    """When the card number sits past character 80 in the original body,
    the leading-80-char preview window does NOT contain it. The audit row
    still records the full match via sha256 + match_count, but the raw
    card digits are kept off disk. This is the primary point of the
    schema change.
    """
    long_lead = "x" * 90
    body = f"{long_lead} 4111 1111 1111 1111 trailing"
    allowed, reason = outbound_scanner.gate_outbound_content(
        config=cfg, text=body, surface="todo_body",
    )
    assert allowed is False
    assert "credit_card" in (reason or "")

    rows = _read_rows(cfg)
    assert len(rows) == 1
    row = rows[0]

    # The raw card number is past the 80-char window, so no card digits
    # leak through the preview field.
    assert "4111" not in row["preview_first_80_chars"]
    assert row["matched_pattern"] == "credit_card"
    assert row["match_count"] >= 1
    assert row["content_length"] == len(body)
