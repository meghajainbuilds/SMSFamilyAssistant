# coordination skills (BEHAVIOR-only)

The skill files live at `kavi-runtime/skills/` (the deploy-rsync canonical
home). This directory is the entry-point pointer:

- Intent classifier: `kavi-runtime/skills/coordination_intent_classifier.md`
- Addressee message composer: `kavi-runtime/skills/coordination_addressee_message_composer.md`
- Reply parser: `kavi-runtime/skills/coordination_reply_parser.md`
- Outcome composer: `kavi-runtime/skills/coordination_outcome_composer.md`

When a coordination behavior bug fires, open the skill file for the
composer in question — voice + structural rules are NOT duplicated there
(per the three-axis isolation test).
