# kavi-persona skills (BEHAVIOR-only)

The skill files live at `kavi-runtime/skills/` (the deploy-rsync canonical
home). This directory is the entry-point pointer.

The architectural test at
`kavi-runtime/tests/test_composer_skill_isolation.py` asserts that:

1. No skill file re-encodes voice rules (those live in
   `capabilities/kavi-persona.md`).
2. No skill file re-encodes structural rules with hardcoded numeric
   values (those live in `kavi_runtime/structural_checks.py` as
   `LENGTH_CAP_TARGET` etc., referenced by name).

Pilot composer: `periodic_summary_composer.md` (the first skill refactored
under the three-axis split). Remaining ~12 skills are queued for the same
treatment (Phase 2 extension, see `kavi-runtime/backlog.md`).

## Skill files

- `kavi-runtime/skills/periodic_summary_composer.md`
- `kavi-runtime/skills/qa_question_composer.md`
- `kavi-runtime/skills/post_action_reply_composer.md`
- `kavi-runtime/skills/action_clarifying_reply_composer.md`
- `kavi-runtime/skills/action_intent_classifier.md`
- `kavi-runtime/skills/action_target_matcher.md`
- `kavi-runtime/skills/pending_clarification_resolver.md`
- `kavi-runtime/skills/pause_intent_classifier.md`
- `kavi-runtime/skills/reply_intent_parser.md` (intent-first dispatch, 2026-06-10)
- `kavi-runtime/skills/kavi_reply_composer.md` (intent-first dispatch, 2026-06-10)
<!-- qa_reply_classifier.md DELETED 2026-06-10: fully replaced by reply_intent_parser.md -->
- `kavi-runtime/skills/kavi_conversation.md`
- `kavi-runtime/skills/correction_classifier.md`
- `kavi-runtime/skills/weekly_self_check_composer.md`
- `kavi-runtime/skills/weekly_self_check_classifier.md`
- `kavi-runtime/skills/close_suggestion_judge.md` (2026-06-10, three-axis-clean from birth)
- `kavi-runtime/skills/morning_theme_clusterer.md` (2026-06-10, three-axis-clean from birth)
