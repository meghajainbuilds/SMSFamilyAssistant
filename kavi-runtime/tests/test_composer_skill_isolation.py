"""Architectural test: composer skills own BEHAVIOR only.

The 2026-05-29 EM-critique refactor (Phase 2) split rules across three
axes:

  PERSONA — voice, identity, refusal. Lives in
    `capabilities/kavi-persona.md`. Loaded at compose time via
    `persona_loader`.

  STRUCTURAL CONSTRAINTS — length caps, prose-vs-lists, format. Lives
    in `kavi_runtime/structural_checks.py`. Enforced runtime-post
    via the g_v* / g_p* gates AND surfaced to the LLM via the
    user_msg in `claude_client.py`.

  BEHAVIOR — what each composer surfaces, when to skip, how to anchor.
    Lives in `kavi_runtime/skills/<composer>.md` (and is pointed at by
    the per-capability `capabilities/<name>/skill.md` or `skills/README.md`
    entry-point pointer added by Phase 4 of the refactor).

This test locks the contract: composer skills must NOT re-encode
structural or voice rules. They drift independently from the canonical
sources and the parent-assoc f-string fallback class of bugs comes
right back.

Phase 6 (2026-06-02) extended the test to iterate every skill file
under `kavi-runtime/skills/` automatically. Skills that have not yet
been refactored under the three-axis split must be named in
`KNOWN_UNREFACTORED_SKILLS` until they're migrated. Adding a new skill
without either refactoring it or adding it to the allowlist will fail
the test — this is the forcing function for the Phase 2 extension work.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

# Composers that have been refactored under the three-axis split.
# Phase 2 pilots the periodic_summary composer; other composers join as
# they're refactored. To migrate a skill OFF the unrefactored list:
# strip voice/structural rules from the skill body, ensure user_msg in
# claude_client.py references structural_checks constants, and ensure
# persona_loader is wired in for that composer.
REFACTORED_COMPOSER_SKILLS: set[str] = {
    "periodic_summary_composer.md",
    # Born three-axis-clean (2026-06-10): behavior-only judgment skills;
    # caps live in capabilities/kavi_persona/{close_suggestions,selection}.py
    # and structural_checks.py, referenced by name only.
    "close_suggestion_judge.md",
    "morning_theme_clusterer.md",
    # Born three-axis-clean (2026-06-10, intent-first dispatch rebuild):
    # the parser + final reply composer own behavior only; caps surface
    # via structural_checks constants in the user_msg.
    "reply_intent_parser.md",
    "kavi_reply_composer.md",
}

# Skills not yet refactored — allowed to contain voice/structural
# rules until the Phase 2 extension lands per-skill. Each entry here is
# a deliberate debt declaration; the count should trend toward zero.
KNOWN_UNREFACTORED_SKILLS: set[str] = {
    "weekly_self_check_composer.md",
    # qa_reply_classifier.md DELETED 2026-06-10 (intent-first dispatch
    # rebuild) — classifier fully replaced by reply_intent_parser.md.
    "action_target_matcher.md",
    "pause_intent_classifier.md",
    "kavi_conversation.md",
    "action_clarifying_reply_composer.md",
    "email_to_tasks.md",
    "coordination_outcome_composer.md",
    "pending_clarification_resolver.md",
    "weekly_self_check_classifier.md",
    "coordination_reply_parser.md",
    "qa_question_composer.md",
    "coordination_intent_classifier.md",
    "correction_classifier.md",
    "coordination_addressee_message_composer.md",
    "task_writer_mstodo.md",
    "post_action_reply_composer.md",
    "action_intent_classifier.md",
}


def _discover_all_skill_files() -> list[str]:
    """Return every skill .md file under kavi-runtime/skills/."""
    if not SKILLS_DIR.exists():
        return []
    return sorted(
        p.name for p in SKILLS_DIR.iterdir()
        if p.is_file() and p.suffix == ".md" and not p.name.startswith(".")
    )


# Hardcoded length numbers anywhere in a skill file are a structural-rule
# leak. The canonical cap lives in `structural_checks.LENGTH_CAP_TARGET`.
# Any `<=\d+ char`, `≤\d+ char`, `\d+ characters`, `\d+-char` shaped match
# is treated as a leak.
_LENGTH_HARDCODE_PATTERNS = [
    re.compile(r"<=\s*\d+\s*char", re.IGNORECASE),
    re.compile(r"≤\s*\d+\s*char", re.IGNORECASE),
    re.compile(r"\b\d+\s*characters?\b", re.IGNORECASE),
    re.compile(r"\b\d+-char\b", re.IGNORECASE),
]

# Voice-y phrases that belong in persona, not in the per-composer skill.
# Each phrase, when found in a skill body, indicates the skill is doing
# persona's job. Skills should describe BEHAVIOR (what to surface), not
# VOICE (how Kavi sounds in general).
_VOICE_LEAK_PHRASES = [
    "warm note",
    "warmly",
    "first-person",
    "first person",
    "no signoff",
    "formulaic warmth",
    "casual emoji",
    "voice rules",
]


def _read_skill_body(skill_path: Path) -> str:
    """Return the skill text with HTML comments stripped. Comments are
    documentation for engineers; they can mention voice/structural concepts
    without violating the three-axis split."""
    raw = skill_path.read_text()
    return re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)


@pytest.mark.parametrize(
    "skill_filename", sorted(REFACTORED_COMPOSER_SKILLS),
)
def test_skill_does_not_hardcode_length_caps(skill_filename: str) -> None:
    """Composer skills (refactored) must not hardcode length caps. The
    canonical cap is `structural_checks.LENGTH_CAP_TARGET`; user_msg
    references it; structural_checks enforces it. The skill owns behavior,
    not constraints."""
    skill_path = SKILLS_DIR / skill_filename
    assert skill_path.exists(), f"missing skill file: {skill_path}"
    body = _read_skill_body(skill_path)

    leaks: list[str] = []
    for pat in _LENGTH_HARDCODE_PATTERNS:
        for m in pat.finditer(body):
            leaks.append(m.group(0))

    assert not leaks, (
        f"Skill file {skill_filename} hardcodes length caps "
        f"({leaks!r}). Move to `structural_checks.LENGTH_CAP_TARGET` "
        f"and reference from user_msg in claude_client.py instead."
    )


@pytest.mark.parametrize(
    "skill_filename", sorted(REFACTORED_COMPOSER_SKILLS),
)
def test_skill_does_not_leak_voice_rules(skill_filename: str) -> None:
    """Composer skills (refactored) must not re-encode voice rules. Voice
    lives in `capabilities/kavi-persona.md`. The skill owns behavior."""
    skill_path = SKILLS_DIR / skill_filename
    assert skill_path.exists(), f"missing skill file: {skill_path}"
    body = _read_skill_body(skill_path).lower()

    leaks = [phrase for phrase in _VOICE_LEAK_PHRASES if phrase in body]

    assert not leaks, (
        f"Skill file {skill_filename} leaks voice rules "
        f"({leaks!r}). Move to `capabilities/kavi-persona.md` and let "
        f"persona_loader inject voice at compose time."
    )


def test_every_skill_is_classified_refactored_or_unrefactored():
    """Phase 6 forcing function: every skill file under kavi-runtime/skills/
    must appear in either REFACTORED_COMPOSER_SKILLS or
    KNOWN_UNREFACTORED_SKILLS. New skills added without a classification
    fail this test — the engineer adding them must explicitly decide.

    To migrate a skill from KNOWN_UNREFACTORED_SKILLS to
    REFACTORED_COMPOSER_SKILLS:
    1. Strip voice/structural rules from the skill body.
    2. Wire persona_loader for that composer in claude_client.py.
    3. Reference structural_checks constants in user_msg.
    4. Move the filename across the two sets in this file.
    """
    all_skills = set(_discover_all_skill_files())
    classified = REFACTORED_COMPOSER_SKILLS | KNOWN_UNREFACTORED_SKILLS

    unclassified = all_skills - classified
    extra = classified - all_skills

    assert not unclassified, (
        "Skill files exist on disk but are NOT classified in "
        "REFACTORED_COMPOSER_SKILLS or KNOWN_UNREFACTORED_SKILLS:\n"
        + "\n".join(f"  {s}" for s in sorted(unclassified))
        + "\n\nAdd each new skill to one of those sets. If the skill has "
          "been refactored under the three-axis split, add to "
          "REFACTORED_COMPOSER_SKILLS; otherwise add to "
          "KNOWN_UNREFACTORED_SKILLS with the intent to migrate."
    )
    assert not extra, (
        "Classified skills do not exist on disk (stale entries):\n"
        + "\n".join(f"  {s}" for s in sorted(extra))
        + "\n\nRemove from REFACTORED_COMPOSER_SKILLS / "
          "KNOWN_UNREFACTORED_SKILLS."
    )
