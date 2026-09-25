"""Regression test: skill files under `kavi-runtime/skills/` must NOT
contain embedded persona text.

Spec-IS-the-runtime collapse (2026-05-27): persona voice + identity is
loaded at compose time from `capabilities/kavi-persona.md` via
`kavi_runtime/persona_loader.py`. Skill files own task-specific
composer instructions only. If anyone re-adds embedded persona blocks
to a skill file in a future change, this test fails loud — re-creating
the spec-vs-runtime drift class of bug is the failure mode this guard
exists to prevent.

Markers we check for (any one matches → fail):

  - `# === Persona` — the historical marker on the 5 composer skills
    pre-2026-05-27.
  - `## Persona` — H2 form that might creep back in.
  - `^Persona:` line-leader form.

If a NEW persona-bearing skill is added in the future, the right
response is to load persona at runtime, not embed it. If this test ever
needs to allow a marker, document the exception inline; do not silently
relax the assertion.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \
        tests/test_skill_files_have_no_embedded_persona.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

# Forbidden markers, regex per the contract above
_FORBIDDEN = [
    re.compile(r"#\s*===\s*Persona"),
    re.compile(r"^##\s+Persona\b", re.MULTILINE),
    re.compile(r"^Persona:", re.MULTILINE),
]


def test_skill_files_have_no_embedded_persona() -> None:
    """Every .md under skills/ must be free of embedded persona markers."""
    offenders: list[str] = []
    for skill in sorted(SKILLS_DIR.glob("*.md")):
        body = skill.read_text()
        for pat in _FORBIDDEN:
            if pat.search(body):
                offenders.append(f"{skill.name}: matched {pat.pattern!r}")
                break
    assert not offenders, (
        "Skill files re-introduced embedded persona text — collapse this "
        "by removing the block and letting persona_loader load it from "
        "capabilities/kavi-persona.md at compose time.\n"
        + "\n".join(offenders)
    )
