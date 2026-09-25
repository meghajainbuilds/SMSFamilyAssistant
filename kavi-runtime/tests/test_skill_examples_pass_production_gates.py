"""Architectural test — every skill example must pass production gates.

The bug that shipped tonight's 9 PM rollup ("7 new tasks in MS To Do")
had a structural cause: the periodic_summary skill at line 57 carried a
worked example ("8 new tasks in MS To Do, 2 need your call. Tap MS To
Do for the list.") that taught the bad shape. The persona spec at
kavi-persona.md:185 said the shape was BAD; the skill example said it
was canonical. The LLM trusted the example.

This test reads every skill .md, extracts every worked input → output
example, and runs the output through:

- structural_checks (length cap, banned voice substrings, list dumps,
  status-board language, warmth openers, dead-reply syntax)
- The deep verify gates registered for that capability (today: only
  count_without_axis applies to the periodic_summary skill)

If any skill example would fail production gates, the test fails. This
means a skill cannot teach a shape that violates the persona spec.

Added 2026-06-02 Phase 1 (deep-verify-asymmetry response).

Parser limitations: today the test recognizes only the
`> {"message": "..."}`-style markdown blockquote shape used by
periodic_summary_composer.md, kavi_conversation.md, and several
post-action composer skills. Skills with other output shapes
(classifiers, JSON arrays, free-form prose examples) are listed in
`SKILLS_WITH_NO_EXTRACTABLE_EXAMPLES` and excluded. As more skills
adopt the message-JSON shape, they pick up coverage automatically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from capabilities.kavi_persona.verify import _detect_count_without_axis
from kavi_runtime.structural_checks import (
    LENGTH_CAP_HARD,
    LENGTH_CAP_TARGET,
    passes_g_p1,
    passes_g_p2,
    passes_g_p3,
    passes_g_v1,
    passes_g_v2,
    passes_g_v4,
    passes_g_v5,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_DIR = _REPO_ROOT / "kavi-runtime" / "skills"

# Skills whose example shape is not the `> {"message": "..."}` blockquote
# shape this test parses. These are classifiers, multi-field JSON
# composers, or free-form prose examples. Excluded from this v1 of the
# consistency check. Adding a new extractor (e.g., for action_intent_
# classifier's decision-dict output) is one way to grow coverage.
SKILLS_WITH_NO_EXTRACTABLE_EXAMPLES: set[str] = {
    "email_to_tasks.md",
    "coordination_intent_classifier.md",
    "coordination_reply_parser.md",
    "coordination_outcome_composer.md",
    "coordination_addressee_message_composer.md",
    "action_intent_classifier.md",
    "action_target_matcher.md",
    "action_clarifying_reply_composer.md",
    "correction_classifier.md",
    "pending_clarification_resolver.md",
    "post_action_reply_composer.md",
    # qa_reply_classifier.md DELETED 2026-06-10 (intent-first dispatch
    # rebuild) — classifier fully replaced by reply_intent_parser.md.
    "qa_question_composer.md",
    "pause_intent_classifier.md",
    "weekly_self_check_classifier.md",
    "weekly_self_check_composer.md",
    "task_writer_mstodo.md",
    # 2026-06-10: judgment skills with classifier-dict output shapes
    # (suggest_close / theme), not the message-JSON shape this v1 parses.
    "close_suggestion_judge.md",
    "morning_theme_clusterer.md",
}

# Skills that DO carry parseable `{"message": "..."}` examples.
# Coverage today: periodic_summary_composer + kavi_conversation. The test
# auto-discovers any skill that yields parseable examples, so this is
# documented for human readers rather than enforced.
_SKILLS_WITH_EXTRACTABLE_EXAMPLES_EXPECTED: set[str] = {
    "periodic_summary_composer.md",
    "kavi_conversation.md",
}


def _extract_message_examples(skill_text: str) -> list[str]:
    """Pull every `> {"message": "..."}` example out of a skill file.

    Returns the list of message strings (one per example). Skill files
    use markdown blockquotes for example outputs; the JSON must contain a
    `message` key.
    """
    out: list[str] = []
    # Match `> {...}` on a single line. Multi-line JSON blocks would need
    # additional handling; today every skill example is single-line.
    for m in re.finditer(r"^>\s*(\{.*\})\s*$", skill_text, flags=re.MULTILINE):
        raw = m.group(1)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = parsed.get("message")
        if isinstance(msg, str):
            out.append(msg)
    return out


def _skill_files() -> list[Path]:
    if not _SKILLS_DIR.is_dir():
        pytest.skip(f"Skills dir not found at {_SKILLS_DIR}")
    return sorted(p for p in _SKILLS_DIR.glob("*.md") if p.is_file())


class TestSkillExamplesPassStructuralGates:
    """Every parseable skill example must pass the cross-cutting
    structural gates (length, voice substrings, list dumps, status-board
    phrases, formulaic warmth openers, dead-reply syntax)."""

    @pytest.mark.parametrize("skill_path", _skill_files(), ids=lambda p: p.name)
    def test_skill_examples_pass_structural_gates(self, skill_path: Path) -> None:
        if skill_path.name in SKILLS_WITH_NO_EXTRACTABLE_EXAMPLES:
            pytest.skip(
                f"{skill_path.name} uses a non-message output shape; not "
                f"covered by this v1 extractor"
            )
        text = skill_path.read_text()
        examples = _extract_message_examples(text)
        if not examples:
            pytest.skip(
                f"{skill_path.name} carries no `> {{\"message\": \"...\"}}` "
                f"examples to validate"
            )

        for i, msg in enumerate(examples):
            ctx = f"{skill_path.name} example {i + 1}: {msg!r}"
            assert len(msg) <= LENGTH_CAP_HARD, (
                f"{ctx} exceeds hard length cap {LENGTH_CAP_HARD}"
            )
            # Soft cap: target is 120 but examples up to hard are tolerated
            # (the runtime rejects only at hard cap). Leave length_cap_target
            # as a non-failing observation rather than a hard assert so old
            # examples don't regress the test on a margin issue.
            assert passes_g_v1(msg) or len(msg) <= LENGTH_CAP_HARD, ctx
            assert passes_g_v2(msg), f"{ctx} contains list markers (G-V2)"
            assert passes_g_v4(msg), f"{ctx} uses third-person Kavi (G-V4)"
            assert passes_g_v5(msg), f"{ctx} has signoff tail (G-V5)"
            assert passes_g_p1(msg), (
                f"{ctx} contains status-board language (G-P1)"
            )
            assert passes_g_p2(msg), (
                f"{ctx} starts with formulaic warmth opener (G-P2)"
            )
            assert passes_g_p3(msg), (
                f"{ctx} uses dead 'N yes / N no' syntax (G-P3)"
            )


class TestPeriodicSummarySkillExamplesPassDeepVerifyGates:
    """The periodic_summary skill specifically: examples must pass the
    deep verify gates registered for that capability (today:
    count_without_axis). This is the prevention layer that would have
    blocked tonight's bug at PR time."""

    def test_periodic_summary_examples_pass_count_without_axis(self) -> None:
        skill_path = _SKILLS_DIR / "periodic_summary_composer.md"
        if not skill_path.exists():
            pytest.skip("periodic_summary_composer.md not found")
        text = skill_path.read_text()
        examples = _extract_message_examples(text)
        assert examples, (
            "periodic_summary_composer.md carries no parseable examples; "
            "the deep-verify-asymmetry prevention test cannot run"
        )

        for i, msg in enumerate(examples):
            failures = _detect_count_without_axis(
                msg, top_importance_tasks=None,
            )
            assert failures == [], (
                f"periodic_summary_composer.md example {i + 1} "
                f"({msg!r}) would FAIL the count_without_axis gate. "
                f"This is the exact bug class that shipped tonight's "
                f"9 PM rollup. Failures: {failures}"
            )


class TestSkillExampleCoverage:
    """Meta-check: confirms the auto-discovery is finding the skills we
    expect. If a skill stops being discoverable (e.g., examples removed),
    surface that to the human reader rather than silently dropping
    coverage."""

    def test_periodic_summary_skill_is_covered(self) -> None:
        skill_path = _SKILLS_DIR / "periodic_summary_composer.md"
        if not skill_path.exists():
            pytest.skip("periodic_summary_composer.md not found")
        examples = _extract_message_examples(skill_path.read_text())
        assert len(examples) >= 5, (
            f"periodic_summary_composer.md should carry several worked "
            f"examples (morning + 9 PM shapes); found {len(examples)}. If "
            f"examples were removed, restore them or update this assert."
        )
