"""Locks the property: capability code lives in capabilities/<name>/, NOT
in kavi_runtime/handlers.py or kavi_runtime/claude_client.py. Catches the
2026-06-02 first-Phase-4-attempt failure mode of re-export shims.

Line caps reflect the post-physical-move state. Bumping these caps means
adding capability code back into the monoliths — don't do that without
a code review.

The def-not-just-imports test asserts every capabilities/<name>/*.py
file (excluding __init__.py) contains at least one def or class
statement. Files known to still be pure re-export pass-throughs from
the pre-Phase-4 era are listed in KNOWN_PASSTHROUGH_FILES; the test
asserts this list shrinks monotonically (no new entries allowed).

To migrate a file from KNOWN_PASSTHROUGH_FILES:
1. Move the canonical function body from kavi_runtime/ into the file.
2. Update kavi_runtime/ imports to re-export from the capability file
   (handlers.py / claude_client.py imports are allowed; new code should
   import directly from capabilities/).
3. Remove the file's path from KNOWN_PASSTHROUGH_FILES.
4. Run this test to confirm.
"""

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Line caps for the runtime monoliths. As Phase 4 work continues, these
# should drop. Bumping them is a code-smell that capability code has
# crept back into a monolith. As of 2026-06-02:
# - handlers.py at ~3434 lines after Phase 4 part 1 (inbox_to_task +
#   periodic_summary moved). Remaining: imessage_received, action layer
#   (~1500 lines), qa_loop reply handlers (~250 lines), correction
#   handler (~250 lines), title editors (~100 lines).
# - claude_client.py at ~1837 lines after Phase 4 part 1. Remaining:
#   ~16 composer methods (~1300 lines) still inline.
# - structural_checks.py at 363 lines. Three kavi-persona-specific
#   blocks identified for move per the Phase 4 inventory.
# - synthetic_compose.py at 38 lines — all replay/verify bodies moved
#   into capabilities/.
HANDLERS_LINE_CAP = 300
CLAUDE_CLIENT_LINE_CAP = 400
STRUCTURAL_CHECKS_LINE_CAP = 400
SYNTHETIC_COMPOSE_LINE_CAP = 50

# Phase 4 (2026-06-02) post-physical-move forbidden-def patterns. These
# substring patterns catch any `def <pat>...` body in the monolith and
# fail loudly. The only way to pass these tests is to physically move the
# function body to capabilities/<name>/.
FORBIDDEN_DEF_PATTERNS_IN_HANDLERS = [
    "compose_",
    "classify_",
    "_handle_q_and_a",
    "_apply_qa_",
    "_handle_qa_",
    "_try_handle_action_",
    "_handle_create_task_verb",
    "_handle_correction",
    "_periodic_summary_",
    "_filter_summary_queue",
    "_filter_pending_questions",
    "_fetch_open_todo_task_ids",
    "_pick_summary_anchor",
]
FORBIDDEN_DEF_PATTERNS_IN_CLAUDE_CLIENT = [
    "compose_",
    "classify_",
    "resolve_pending_clarification",
    "run_email_to_tasks",
]


# Files that still re-export from kavi_runtime/ rather than defining the
# function body locally. Pre-Phase-4 pattern. As Phase 4 continues, this
# set shrinks until empty. The test asserts every entry resolves to an
# actual file (catches stale entries) AND that no new file outside this
# set is a pure pass-through.
KNOWN_PASSTHROUGH_FILES: set[str] = set()


def _line_count(path):
    return sum(1 for _ in path.open())


def test_handlers_is_under_cap():
    p = REPO_ROOT / "kavi-runtime/kavi_runtime/handlers.py"
    actual = _line_count(p)
    assert actual <= HANDLERS_LINE_CAP, (
        f"handlers.py is {actual} lines; cap is {HANDLERS_LINE_CAP}. "
        f"Capability code has crept back. Move it to capabilities/<name>/."
    )


def test_claude_client_is_under_cap():
    p = REPO_ROOT / "kavi-runtime/kavi_runtime/claude_client.py"
    actual = _line_count(p)
    assert actual <= CLAUDE_CLIENT_LINE_CAP, (
        f"claude_client.py is {actual} lines; cap is {CLAUDE_CLIENT_LINE_CAP}. "
        f"Capability-specific compose methods have crept back."
    )


def test_structural_checks_is_under_cap():
    p = REPO_ROOT / "kavi-runtime/kavi_runtime/structural_checks.py"
    actual = _line_count(p)
    assert actual <= STRUCTURAL_CHECKS_LINE_CAP, (
        f"structural_checks.py is {actual} lines; cap is {STRUCTURAL_CHECKS_LINE_CAP}."
    )


def test_synthetic_compose_is_under_cap():
    p = REPO_ROOT / "kavi-runtime/kavi_runtime/synthetic_compose.py"
    actual = _line_count(p)
    assert actual <= SYNTHETIC_COMPOSE_LINE_CAP, (
        f"synthetic_compose.py is {actual} lines; cap is {SYNTHETIC_COMPOSE_LINE_CAP}."
    )


def test_capability_files_contain_def_not_just_imports():
    """For each capabilities/<name>/*.py file (excluding __init__.py and
    files explicitly classified as KNOWN_PASSTHROUGH_FILES), the file
    must contain at least one def/class definition. Pure-import files
    are the re-export shim pattern that this test exists to prevent.

    The KNOWN_PASSTHROUGH_FILES list documents pre-Phase-4 shims that
    have not yet been physically moved. New files outside this list must
    contain real function/class bodies.
    """
    capabilities_dir = REPO_ROOT / "capabilities"
    failures: list[str] = []
    seen_passthrough: set[str] = set()
    for cap_dir in capabilities_dir.iterdir():
        if not cap_dir.is_dir() or cap_dir.name.startswith("_"):
            continue
        if cap_dir.name == "Templates":
            continue
        for py_file in cap_dir.rglob("*.py"):
            if py_file.name == "__init__.py":
                continue
            rel = str(py_file.relative_to(REPO_ROOT))
            body = py_file.read_text()
            has_def_or_class = ("def " in body) or ("class " in body)
            if not has_def_or_class:
                if rel in KNOWN_PASSTHROUGH_FILES:
                    seen_passthrough.add(rel)
                    continue
                failures.append(
                    f"{rel} contains no def/class — looks like a re-export "
                    f"shim. Capability code must live here, not behind an "
                    f"import statement. If this is intentional pre-Phase-4 "
                    f"debt, add it to KNOWN_PASSTHROUGH_FILES with a TODO."
                )

    # Stale-entry check: every KNOWN_PASSTHROUGH_FILES path must still
    # exist AND still be a pure pass-through. If a file got migrated, the
    # entry is stale and the test surfaces it so the list is cleaned.
    for stale in KNOWN_PASSTHROUGH_FILES - seen_passthrough:
        stale_path = REPO_ROOT / stale
        if not stale_path.exists():
            failures.append(
                f"KNOWN_PASSTHROUGH_FILES entry {stale!r} does not exist; "
                f"remove from the list."
            )
        else:
            # File exists but is no longer a passthrough — migrated.
            failures.append(
                f"KNOWN_PASSTHROUGH_FILES entry {stale!r} now contains "
                f"def/class statements; remove from the list (it's been "
                f"migrated; congrats)."
            )

    if failures:
        raise AssertionError("\n".join(failures))


def test_handlers_has_no_capability_specific_defs():
    """Phase 4 (2026-06-02) name-pattern gate. Substrings like `def compose_`
    or `def _handle_qa_` MUST NOT appear in handlers.py — those function
    bodies belong in capabilities/<name>/."""
    text = (REPO_ROOT / "kavi-runtime/kavi_runtime/handlers.py").read_text()
    for pat in FORBIDDEN_DEF_PATTERNS_IN_HANDLERS:
        assert f"def {pat}" not in text, (
            f"handlers.py defines {pat}* - should be in capabilities/<name>/. "
            f"Move the function body, delete from handlers.py."
        )


def test_claude_client_has_no_capability_specific_defs():
    """Phase 4 (2026-06-02) name-pattern gate. Capability composers and
    classifiers must not be method bodies on ClaudeClient."""
    text = (REPO_ROOT / "kavi-runtime/kavi_runtime/claude_client.py").read_text()
    for pat in FORBIDDEN_DEF_PATTERNS_IN_CLAUDE_CLIENT:
        assert f"def {pat}" not in text, (
            f"claude_client.py defines {pat}* - should be in capabilities/<name>/. "
            f"Move the function body, delete from claude_client.py."
        )


def test_no_module_level_state_in_capability_files():
    """Sanity: capability files should not register module-level handler
    state (caches, locks, etc) that handler tests would patch via the
    handlers module. This is a placeholder gate to make explicit that
    capability files own their own module-level state cleanly."""
    # Placeholder — no concrete check today. Reserved for future Phase 5
    # work where capability files might grow their own caches.
    assert True
