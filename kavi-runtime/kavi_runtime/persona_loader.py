"""Load Kavi's persona text from the canonical spec at
`capabilities/kavi-persona.md` so the runtime composers and the spec are
always the same artifact.

Before this module existed, the persona (voice rules, three character
traits, refusal language, identity) was COPY-PASTED into the bottom of
each composer skill file under `kavi-runtime/skills/`. When Megha edited
the spec, runtime behavior did not change because the runtime never read
the spec — only its frozen snapshots in the skill files. That class of
bug ("spec-vs-runtime drift") is what this loader closes.

Contract:

  - One public function, `load_persona_text()`, returns the runtime-
    persona text extracted from the spec. It includes only the sections
    that should constrain Kavi's voice: the `## Behavior` block (with all
    its per-rule sub-blocks) and the `## System prompt` block.
  - Sections meant for human readers — Vision, Principles, Onboarding
    plan, Open questions, Why now, Metrics, Architecture, Changelog,
    Out of scope, TL;DR — are intentionally excluded.
  - Cached per process after first read. The spec is ~30 KB and read on
    every composer call; the Anthropic prompt cache handles repetition
    cheaply, but holding the parsed text in memory avoids the disk I/O.
  - Loud failure if the spec is missing or the Behavior section can't be
    found. Returning empty silently would let the runtime ship without
    its persona, which is worse than crashing the composer call.

Section parser:

  The spec uses `## H2` for top-level structure. We extract the two
  sections we care about by scanning for `^## Behavior` and
  `^## System prompt` headers, taking everything until the next H2 of
  equal level (or end of file). H3 / H4 sub-blocks inside the matched
  section are kept verbatim — they ARE the per-rule definitions the
  composer needs.

  Edge cases handled:
    - Trailing whitespace on header lines
    - H1 / H3+ headers interleaved between H2 sections (we only stop on
      another `^## `)
    - Either section may be absent: missing `## Behavior` is fatal
      (the persona has nothing to say); missing `## System prompt`
      logs a warning but the loader returns the Behavior block alone.

Where the file lives on disk:

  The kavi-runtime/ folder is rsync'd to Kavi's Mac at
  `/Users/kavi/kavi-runtime/`. The persona spec lives at the repo root,
  `capabilities/kavi-persona.md`, which is NOT inside kavi-runtime/.
  `scripts/deploy.sh` adds a fourth rsync block that ships
  `capabilities/` to `$KAVI_RUNTIME_DIR/capabilities/`. The loader reads
  from a path passed in by `ClaudeClient` (config-driven) so the same
  module works on Megha's Mac (repo-root path) and Kavi's Mac
  (kavi-runtime-relative path) without code changes.

This module replaces the pattern of embedding persona text in skill
files. Skill files should now contain task-specific composer
instructions ONLY (e.g., "compose a periodic summary <=120 chars");
voice and identity come from the spec.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from kavi_runtime.private_overlay import read_resolved

logger = logging.getLogger(__name__)


# Process-wide cache. Keyed by the absolute path of the spec so a test
# can swap paths and still see fresh parses. `_lock` guards concurrent
# reads on the runtime's webhook-driven request paths.
_CACHE: dict[str, str] = {}
_LOCK = threading.Lock()


# Headings we extract. Order matters for the assembled output: Behavior
# (the per-rule rubric) comes first, then System prompt (the XML-tagged
# few-shots Anthropic sees verbatim).
_BEHAVIOR_HEADER = re.compile(r"^##\s+Behavior\s*$", re.MULTILINE)
_SYSTEM_PROMPT_HEADER = re.compile(r"^##\s+System prompt\s*$", re.MULTILINE)
# Any H2 that is NOT the section we're currently extracting — used to find
# the end of the matched section.
_ANY_H2 = re.compile(r"^##\s+\S", re.MULTILINE)


def _extract_section(text: str, header_re: re.Pattern[str]) -> str | None:
    """Return the section body starting at `header_re`'s first match up
    to (but not including) the next H2 header, or None if not found.

    The returned text starts with the H2 line itself so downstream
    consumers can see which section they're in (helps debugging when
    the assembled prompt is logged into a trace).
    """
    m = header_re.search(text)
    if not m:
        return None
    start = m.start()
    # Find the next H2 after this one. If none, take to end of file.
    end_search = _ANY_H2.search(text, pos=m.end())
    end = end_search.start() if end_search else len(text)
    return text[start:end].rstrip() + "\n"


def load_persona_text(persona_md_path: str | Path) -> str:
    """Return the runtime-persona text for Kavi composer calls.

    Extracted from `capabilities/kavi-persona.md`: the `## Behavior`
    section (per-rule rubric) plus the `## System prompt` section (XML-
    tagged identity + voice + refusal). Cached per-process after first
    read; subsequent calls return the cached string without re-parsing.

    Args:
      persona_md_path: filesystem path to the persona spec markdown.
        Passed in by `ClaudeClient` from `config.paths.kavi_persona_md`
        so tests and prod can wire different paths.

    Returns:
      The assembled persona text. Header order: Behavior then System
      prompt. Each section is preceded by its own `## ` header so the
      composer's LLM sees the structure.

    Raises:
      FileNotFoundError: spec file missing on disk. The runtime cannot
        compose Kavi messages without persona text, so we fail loudly
        rather than silently shipping voiceless output.
      ValueError: the `## Behavior` section can't be found in the spec.
        A spec without Behavior is malformed; refusing to start beats
        sending Kavi-as-empty-template into iMessage.
    """
    path = Path(persona_md_path).resolve()
    key = str(path)
    # Fast path: cached. The first-read sequence below is racy across
    # threads in pathological reload scenarios, but the worst case is
    # two threads parsing the same file once each, then converging on
    # the same cached string. Acceptable.
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        if not path.exists():
            raise FileNotFoundError(
                f"persona spec not found at {path}; runtime cannot compose "
                "Kavi messages without it. Check that "
                "capabilities/kavi-persona.md is rsync'd to Kavi (see "
                "scripts/deploy.sh)."
            )
        raw = read_resolved(path)
        behavior = _extract_section(raw, _BEHAVIOR_HEADER)
        if behavior is None:
            raise ValueError(
                f"persona spec at {path} is missing the '## Behavior' "
                "section; refusing to compose voiceless Kavi output. "
                "Check the spec's heading structure."
            )
        system_prompt = _extract_section(raw, _SYSTEM_PROMPT_HEADER)
        if system_prompt is None:
            logger.warning(
                "persona_loader: no '## System prompt' section in %s — "
                "shipping Behavior block alone. Verify the spec.",
                path,
            )
            assembled = behavior.strip() + "\n"
        else:
            assembled = behavior.strip() + "\n\n" + system_prompt.strip() + "\n"
        _CACHE[key] = assembled
        return assembled


def _reset_cache_for_test() -> None:
    """Clear the per-process cache. Test-only helper; production code
    must not call this. Provided so tests that rewrite fixture spec
    files between calls observe fresh parses.
    """
    with _LOCK:
        _CACHE.clear()
