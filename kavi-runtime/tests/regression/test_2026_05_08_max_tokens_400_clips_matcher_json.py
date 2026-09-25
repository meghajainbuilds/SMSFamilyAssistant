"""Regression test: matcher response clipped mid-JSON because
max_tokens=400 was too small for fenced output + 4 long task IDs.
Fixed in 6015776 (max_tokens 400 -> 1500, ask for raw JSON).

User-visible failure (pre-fix): "Mark all elders tea items done" hit
parse_error -> 'no Elders' Tea tasks open' lie.

Post-fix expectation: the matcher's max_tokens is at least 1500, the
real response shape parses cleanly, and the pre-fix clipped output
exposes the bug shape (extract_json returns None on it).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from kavi_runtime.claude_client import ClaudeClient


@pytest.mark.regression_fixture_id("2026-05-08_max_tokens_400_clips_matcher_json")
def test_max_tokens_400_clips_matcher_json(regression_fixture) -> None:
    fx = regression_fixture

    # 1. extract_json on the post-fix full response parses to dict shape.
    post_fix_text = fx["raw_llm_output_post_fix_full_response"]
    parsed = ClaudeClient._extract_json(post_fix_text)
    assert parsed is not None and isinstance(parsed, dict)
    assert parsed["match_id"] is None
    assert len(parsed["candidate_task_ids"]) == 4

    # 2. extract_json on the pre-fix clipped response returns None
    #    (proves the failure mode is real and that the fix is required;
    #    NOT a tolerance regression — extract_json should reject mid-JSON
    #    truncation rather than silently swallow it).
    pre_fix_clipped = fx["raw_llm_output_pre_fix_clipped_400_tokens"]
    parsed_clipped = ClaudeClient._extract_json(pre_fix_clipped)
    assert parsed_clipped is None, (
        "extract_json returned a value for clipped JSON; the fix-bundle bar "
        "is fail-safe (None -> caller asks Megha) so silent acceptance is a regression"
    )

    # 3. The match_target_to_open_task call body uses max_tokens >= 1500.
    # Phase 4 (2026-06-02): function body moved to
    # capabilities/kavi_persona/actions/target_matcher.py.
    from capabilities.kavi_persona.actions.target_matcher import (
        match_target_to_open_task,
    )
    src = inspect.getsource(match_target_to_open_task)
    assert "max_tokens=1500" in src or "max_tokens = 1500" in src, (
        "match_target_to_open_task must use max_tokens=1500 (post 6015776). "
        "Reverting below 1500 reproduces the 2026-05-08T18:54Z clip."
    )
