"""Regression test: agents rsynced kavi_runtime/ + tests/ but skipped
skills/ for 24+ hours; runtime restarted on cef1817 with no skill files
on disk. ClaudeClient._skill() raises FileNotFoundError mid-classify.

User-visible failure (pre-fix): every iMessage Megha sent triggered an
exception in the runtime; she saw silence, then alert emails about
classify_action_intent failing.

Post-fix expectation (assertion in this test): the deploy verification
harness catches a missing skill file before the runtime tries to use
it. We assert two things:

  1. The skill loader raises FileNotFoundError when called with a name
     whose .md file is missing (proves the failure mode is real and not
     swallowed).
  2. Every skill in the fixture's skills_required_at_runtime list is
     present in the source skills/ directory today. The deploy script
     in Investment 2 enforces hash-match on Kavi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kavi_runtime.claude_client import ClaudeClient


SKILLS_SRC = (
    Path(__file__).resolve().parents[2]
    / "skills"
)


def _make_loader(skills_dir: Path) -> ClaudeClient:
    """Build a ClaudeClient bypassing __init__'s API-key requirement so
    we can exercise just the skill loader."""
    client = ClaudeClient.__new__(ClaudeClient)
    client._skills_dir = skills_dir
    return client


@pytest.mark.regression_fixture_id("2026-05-08_skills_not_deployed_filenotfounderror")
def test_skill_loader_raises_when_file_absent(
    regression_fixture, tmp_path: Path,
) -> None:
    """If the file does not exist, _skill() raises FileNotFoundError.
    No silent fallback to '' — that would let the runtime call
    Anthropic with a system prompt missing the skill rubric, which is
    worse than failing fast."""
    empty_dir = tmp_path / "skills"
    empty_dir.mkdir()
    client = _make_loader(empty_dir)
    with pytest.raises(FileNotFoundError):
        client._skill("action_intent_classifier")


@pytest.mark.regression_fixture_id("2026-05-08_skills_not_deployed_filenotfounderror")
def test_every_required_skill_file_present_in_source(
    regression_fixture,
) -> None:
    """The fixture lists every skill name the runtime calls. All must
    exist in source skills/ — the deploy verifier asserts hash-match
    against Kavi separately. If a new skill is added or a name is
    renamed, this assertion forces the fixture to be updated alongside
    the code change."""
    missing: list[str] = []
    for skill_filename in regression_fixture["skills_required_at_runtime"]:
        if not (SKILLS_SRC / skill_filename).exists():
            missing.append(skill_filename)
    assert not missing, (
        f"skills/ missing: {missing}. Either restore the file or update "
        f"the fixture's skills_required_at_runtime list."
    )
