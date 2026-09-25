#!/usr/bin/env python3
"""Fingerprint every spec-derived piece of Kavi's system prompts.

The system prompt is assembled from: one skill file, household.md, the
inbox-to-task Behavior section, the persona text and the security baseline
(kavi_runtime/runtime/system_prompt.py). If each piece hashes the same before
and after a change, every assembled prompt is byte-identical.

Uses the runtime's own loaders, so it measures what Kavi actually sends.
Runs on code with or without the private overlay (public-repo step 2 gate).

    python scripts/prompt_hashes.py [config.yaml] > hashes.txt
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))

from kavi_runtime.persona_loader import _extract_section, load_persona_text  # noqa: E402
from kavi_runtime.security_baseline import load_security_baseline_text  # noqa: E402

try:
    from kavi_runtime.private_overlay import read_resolved
except ImportError:  # pre-overlay code
    def read_resolved(p):
        return Path(p).read_text()


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main() -> None:
    cfg = yaml.safe_load(Path(sys.argv[1] if len(sys.argv) > 1 else RUNTIME / "config.yaml").read_text())
    paths = cfg["paths"]
    import re
    itt = read_resolved(paths["inbox_to_task_md"])
    behavior = _extract_section(itt, re.compile(r"^## Behavior\b.*$", re.MULTILINE)) or itt
    print(f"inbox_to_task_behavior {h(behavior)}")
    print(f"persona {h(load_persona_text(paths['kavi_persona_md']))}")
    print(f"security_baseline {h(load_security_baseline_text(paths['security_baseline_md']))}")
    for skill in sorted(Path(paths["skills_dir"]).glob("*.md")):
        print(f"skill:{skill.stem} {h(read_resolved(skill))}")


if __name__ == "__main__":
    main()
