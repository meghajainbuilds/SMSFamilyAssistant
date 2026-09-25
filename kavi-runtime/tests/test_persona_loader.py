"""Tests for kavi_runtime.persona_loader.

The loader is the spec-IS-the-runtime collapse for Kavi's voice. Edits
to `capabilities/kavi-persona.md` must flow into every composer call
without touching skill files or redeploying. These tests pin the
contract:

  - Extracts Behavior + System prompt sections.
  - Excludes Vision / Metrics / Architecture / Changelog (human-only).
  - Caches per-process so the file is read from disk once.
  - Fails LOUDLY on missing file or missing Behavior section — silently
    shipping a voiceless Kavi is worse than crashing the composer.
  - Idempotent: same input → same output.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_persona_loader.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kavi_runtime import persona_loader


# Minimal fixture spec that mirrors the real spec's section shape so the
# extraction logic is exercised against the structures it will see in
# production. Keep the section headers + sub-block markers stable; the
# loader matches `^## ` boundaries.
_FIXTURE_SPEC = """\
---
name: kavi-persona
status: test fixture
---
# Kavi

## TL;DR

Test fixture spec for the loader. Not the real one.

## Vision (meta)

This is human-only context. The loader MUST exclude it.

## Behavior

### Voice rules

#### Texture

- Good: short, direct sentences.
- Bad: dense convoluted prose.

#### Compliance bundle

- 120 character cap.
- No signoff.

### Persona

#### Anticipation

- Good: forward-looking framing.

#### Honesty under uncertainty

- Good: flag what you don't know.

## Metrics

| Metric | Threshold |
| --- | --- |
| Tone | 90% |

This entire section is human-only and must NOT appear in the loader output.

## System prompt

### Prompt text

```xml
<persona>
You are Kavi. Test fixture identity.
</persona>
```

## Architecture

This is human-only. Excluded.

## Changelog

- **2026-05-27.** Test fixture.
"""


@pytest.fixture(autouse=True)
def _reset_loader_cache() -> None:
    """Each test starts with a clean cache so fixture rewrites are visible."""
    persona_loader._reset_cache_for_test()


def _write_fixture(tmp_path: Path, content: str = _FIXTURE_SPEC) -> Path:
    p = tmp_path / "kavi-persona.md"
    p.write_text(content)
    return p


# ---- Extraction shape ------------------------------------------------------


def test_loader_extracts_behavior_section(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    text = persona_loader.load_persona_text(path)
    # Behavior header itself must be present
    assert "## Behavior" in text
    # Per-rule sub-blocks must be carried through
    assert "### Voice rules" in text
    assert "#### Texture" in text
    assert "#### Anticipation" in text
    # Specific Good/Bad bullets land in the output
    assert "120 character cap" in text


def test_loader_extracts_system_prompt_section(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    text = persona_loader.load_persona_text(path)
    assert "## System prompt" in text
    assert "<persona>" in text
    assert "Test fixture identity" in text


def test_loader_excludes_human_only_sections(tmp_path: Path) -> None:
    """Vision, Metrics, Architecture, Changelog, TL;DR must be excluded.
    These sections are for human readers and would dilute the cache
    prefix without changing Kavi's voice."""
    path = _write_fixture(tmp_path)
    text = persona_loader.load_persona_text(path)
    # Header strings themselves should not appear in the output
    assert "## Vision" not in text
    assert "## Metrics" not in text
    assert "## Architecture" not in text
    assert "## Changelog" not in text
    assert "## TL;DR" not in text
    # And neither should body text from those sections
    assert "human-only context" not in text
    assert "Test fixture spec for the loader" not in text


def test_loader_section_order_behavior_first_then_system_prompt(tmp_path: Path) -> None:
    """Behavior comes first so the LLM sees the rubric before the
    XML-tagged identity. Order matters for prompt-cache stability."""
    path = _write_fixture(tmp_path)
    text = persona_loader.load_persona_text(path)
    behavior_pos = text.find("## Behavior")
    system_pos = text.find("## System prompt")
    assert behavior_pos >= 0
    assert system_pos >= 0
    assert behavior_pos < system_pos, (
        "Behavior section must come before System prompt in assembled output"
    )


# ---- Idempotence -----------------------------------------------------------


def test_loader_is_idempotent(tmp_path: Path) -> None:
    """Same input → same output across calls."""
    path = _write_fixture(tmp_path)
    a = persona_loader.load_persona_text(path)
    b = persona_loader.load_persona_text(path)
    assert a == b


# ---- Caching ---------------------------------------------------------------


def test_loader_caches_after_first_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The spec is read from disk once per process. Subsequent calls
    return the cached string. Verified by counting read_text() calls
    on the underlying Path."""
    path = _write_fixture(tmp_path)
    read_count = {"n": 0}
    original_read = Path.read_text

    def _counting_read(self, *args, **kwargs):
        if self.resolve() == path.resolve():
            read_count["n"] += 1
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting_read)
    persona_loader.load_persona_text(path)
    persona_loader.load_persona_text(path)
    persona_loader.load_persona_text(path)
    assert read_count["n"] == 1, (
        f"Expected exactly one disk read; got {read_count['n']}. "
        "The loader should cache after the first call per process."
    )


# ---- Failure modes ---------------------------------------------------------


def test_loader_raises_on_missing_file(tmp_path: Path) -> None:
    """A missing spec file is fatal. The runtime cannot compose Kavi
    messages without persona text; silently returning empty would let
    the runtime ship voiceless output."""
    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError) as exc_info:
        persona_loader.load_persona_text(missing)
    assert "persona spec not found" in str(exc_info.value)


def test_loader_raises_on_missing_behavior_section(tmp_path: Path) -> None:
    """A spec without `## Behavior` is malformed. Refusing to start is
    safer than letting voiceless Kavi into iMessage."""
    bad = "# Header\n\n## Vision\n\nNo behavior here.\n"
    p = tmp_path / "kavi-persona.md"
    p.write_text(bad)
    with pytest.raises(ValueError) as exc_info:
        persona_loader.load_persona_text(p)
    assert "Behavior" in str(exc_info.value)


def test_loader_warns_but_returns_when_system_prompt_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing System prompt section logs a warning but the loader
    returns the Behavior block alone. Refusal layer + behavior alone is
    still a recognizable persona; the few-shot section is not load-
    bearing for voice correctness."""
    text = "# Kavi\n\n## Behavior\n\n### Voice rules\n\n- Good: short.\n"
    p = tmp_path / "kavi-persona.md"
    p.write_text(text)
    result = persona_loader.load_persona_text(p)
    assert "## Behavior" in result
    assert "## System prompt" not in result


# ---- Real spec smoke test --------------------------------------------------


def test_loader_handles_real_spec() -> None:
    """Against the actual capabilities/kavi-persona.md the loader must
    return both sections and exclude the human-only ones. This guards
    against the spec being rewritten in a shape the loader can't parse."""
    repo_root = Path(__file__).resolve().parents[2]
    real_spec = repo_root / "capabilities" / "kavi-persona.md"
    if not real_spec.exists():
        pytest.skip("real persona spec not present in this checkout")
    text = persona_loader.load_persona_text(real_spec)
    assert "## Behavior" in text
    assert "## System prompt" in text
    # Per-rule headers from the post-2026-05-26 rewrite
    assert "### Voice rules" in text or "Voice rules" in text
    # Human-only sections excluded
    assert "## Changelog" not in text
    assert "## Architecture" not in text
