"""Load the security baseline text from the canonical spec at
`capabilities/security-baseline.md` so the runtime composers and the
spec stay the same artifact.

Before this module existed (2026-05-28), the security threat-model prose
— refusal under social-engineering, the categorical never-do list, the
inbound-is-data rule — lived as a Python constant called
`PERSONA_REFUSAL_LAYER` in `kavi_runtime/persona_prompts.py`. The name
was an accident of authoring order: when Kavi was the only surface that
emitted user-facing text, the security prose got bundled into the
persona module. Today the inbox-to-task capability writes to MS To Do
under the same rules, and future capabilities will compose email under
them, so security is cross-cutting and deserves its own canonical
location.

This loader is the second leg of the spec-IS-the-runtime collapse. The
first leg (commit f6ff560, 2026-05-27) moved persona voice + identity
into `capabilities/kavi-persona.md` via `kavi_runtime/persona_loader.py`.
This module mirrors that pattern for security so an edit to the security
spec lands in every composer call automatically.

Contract:

  - One public function, `load_security_baseline_text()`, returns the
    LLM-loaded security prose extracted from the spec's `## System prompt`
    section. That section is verbatim the same text the old
    `PERSONA_REFUSAL_LAYER` constant carried; only the storage location
    moved.
  - Sections meant for human readers — TL;DR, Vision, Principles, Why
    now, Behavior, Metrics, Architecture, Guardrails, Out of scope,
    Changelog — are intentionally excluded. Those describe the rules
    for PM-level review; the prose the LLM needs is the System prompt
    section alone.
  - Cached per-process after first read. The Anthropic prompt cache
    already collapses repetition cheaply at the API level; holding the
    parsed text in memory avoids the disk I/O on every composer call.
  - Loud failure on missing file or missing System prompt section.
    Returning an empty string silently would let the runtime ship
    composer output without the security boundary text, which is worse
    than crashing the composer call.

Section parser:

  Mirrors `kavi_runtime/persona_loader.py`. The spec uses `## H2` for
  top-level structure. We extract `^## System prompt` and take
  everything until the next H2 (or end of file). H3 / H4 sub-blocks
  inside the matched section are kept verbatim — the LLM needs the
  full prose including the "### Prompt text" sub-block and its fenced
  block, because that's where the rules live.

Where the file lives on disk:

  Same shipping path as the persona spec. `scripts/deploy.sh` rsyncs
  `capabilities/` to Kavi's Mac at `/Users/kavi/kavi-runtime/capabilities/`
  (added 2026-05-27 for the persona loader). The security spec ships
  through the same block. `claude_client.ClaudeClient` reads
  `config.paths.security_baseline_md` so tests and prod can wire
  different paths.

This module replaces `kavi_runtime/persona_prompts.PERSONA_REFUSAL_LAYER`.
After this loader is wired into `claude_client.py`, that file is deleted
entirely — a single-constant module with the constant moved out has
nothing left to hold.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from kavi_runtime.private_overlay import read_resolved

logger = logging.getLogger(__name__)


# Process-wide cache. Keyed by the absolute path of the spec so a test
# can swap paths and still see fresh parses. `_LOCK` guards concurrent
# reads on the runtime's webhook-driven request paths.
_CACHE: dict[str, str] = {}
_LOCK = threading.Lock()


# Heading we extract. Only the System prompt section is LLM-loaded; the
# rest of the spec is human-facing context.
_SYSTEM_PROMPT_HEADER = re.compile(r"^##\s+System prompt\s*$", re.MULTILINE)
# Any H2 — used to find the end of the matched section.
_ANY_H2 = re.compile(r"^##\s+\S", re.MULTILINE)


def _extract_section(text: str, header_re: re.Pattern[str]) -> str | None:
    """Return the section body starting at `header_re`'s first match up
    to (but not including) the next H2 header outside a fenced code
    block, or None if not found.

    Why the fenced-block awareness: the security baseline System prompt
    section embeds the LLM-facing prose inside a fenced ``` block, and
    that prose itself uses `## 1.`, `## 2.`, `## 3.` for the three rule
    blocks (verbatim from the old PERSONA_REFUSAL_LAYER constant). A
    naive `^## ` regex would stop at the first rule heading inside the
    fence and truncate the section. We track fence state line-by-line
    and only honor `^## ` boundaries when we're outside a fence.

    The returned text starts with the H2 line itself so downstream
    consumers can see which section they're in (helps debugging when
    the assembled prompt is logged into a trace).
    """
    m = header_re.search(text)
    if not m:
        return None
    start = m.start()
    # Walk forward line-by-line from the header, tracking fenced-code
    # state. The end of the section is the next `^## ` that occurs
    # outside any fence (or end of file).
    lines = text[m.end():].splitlines(keepends=True)
    in_fence = False
    cursor = m.end()
    end = len(text)
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            cursor += len(line)
            continue
        if not in_fence and line.startswith("## ") and len(line) > 3 and not line[3].isspace():
            end = cursor
            break
        cursor += len(line)
    return text[start:end].rstrip() + "\n"


def load_security_baseline_text(path: str | Path | None = None) -> str:
    """Return the LLM-loaded security baseline prose.

    Extracted from `capabilities/security-baseline.md`'s `## System prompt`
    section. Cached after first read per process. Raises loudly on
    missing file or missing System prompt section.

    Args:
      path: filesystem path to the security baseline spec markdown.
        Passed in by `ClaudeClient` from `config.paths.security_baseline_md`
        so tests and prod can wire different paths. Required — the
        runtime cannot compose without the security baseline.

    Returns:
      The System prompt section text. Starts with the `## System prompt`
      H2 line so downstream consumers can see which section they're in
      when the assembled prompt is logged into a trace.

    Raises:
      FileNotFoundError: spec file missing on disk. The runtime cannot
        compose safely without security baseline text, so we fail
        loudly rather than silently shipping a composer call without
        the boundary prose.
      ValueError: the `## System prompt` section can't be found in the
        spec. A spec without it is malformed; refusing to start beats
        sending a composer call with no security text.
    """
    if path is None:
        raise FileNotFoundError(
            "security baseline path not configured; runtime cannot compose "
            "without the security baseline. Set "
            "paths.security_baseline_md in config.yaml."
        )
    resolved = Path(path).resolve()
    key = str(resolved)
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
        if not resolved.exists():
            raise FileNotFoundError(
                f"security baseline spec not found at {resolved}; runtime "
                "cannot compose safely without it. Check that "
                "capabilities/security-baseline.md is rsync'd to Kavi (see "
                "scripts/deploy.sh)."
            )
        raw = read_resolved(resolved)
        system_prompt = _extract_section(raw, _SYSTEM_PROMPT_HEADER)
        if system_prompt is None:
            raise ValueError(
                f"security baseline spec at {resolved} is missing the "
                "'## System prompt' section; refusing to compose without "
                "the security boundary prose. Check the spec's heading "
                "structure."
            )
        assembled = system_prompt.strip() + "\n"
        _CACHE[key] = assembled
        return assembled


def _reset_cache_for_test() -> None:
    """Clear the per-process cache. Test-only helper; production code
    must not call this. Provided so tests that rewrite fixture spec
    files between calls observe fresh parses.
    """
    with _LOCK:
        _CACHE.clear()
