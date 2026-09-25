"""Runtime-written data must never live inside a deploy-synced directory.

deploy.sh mirrors kavi_runtime/, tests/, skills/, capabilities/ and scripts/
with rsync --delete. On 2026-09-23 we found the newsletter pre-filter shadow
log had been written under capabilities/runtime_metrics/ since a Jun 2 file
move (a Path(__file__)-relative path silently changed meaning), so every deploy
erased the evidence needed to switch the filter on. Runtime data paths come
from config (paths.*), which point under /Users/kavi/HomeOS.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY_TARGETS = [
    REPO / "capabilities",
    REPO / "kavi-runtime" / "kavi_runtime",
    REPO / "kavi-runtime" / "skills",
]
# A __file__-anchored path joined with a data-ish directory name.
FORBIDDEN = re.compile(
    r"Path\(__file__\)[^\n]*(runtime_metrics|metrics|evals|state|logs)\b"
)


def test_no_file_relative_runtime_data_paths_in_deploy_targets() -> None:
    offenders = []
    for root in DEPLOY_TARGETS:
        for py in root.rglob("*.py"):
            if "/tests/" in str(py):
                continue
            for n, line in enumerate(py.read_text().splitlines(), 1):
                if FORBIDDEN.search(line):
                    offenders.append(f"{py.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, "runtime data paths must come from config, not __file__:\n" + "\n".join(offenders)


def test_deploy_scripts_exclude_runtime_data_from_capabilities_mirror() -> None:
    for script in ("deploy.sh", "deploy_staging.sh"):
        text = (REPO / "kavi-runtime" / "scripts" / script).read_text()
        assert "--exclude='runtime_metrics/'" in text, script


def test_deploy_refuses_to_restart_during_drain() -> None:
    text = (REPO / "kavi-runtime" / "scripts" / "deploy.sh").read_text()
    guard = text.index("drain_in_progress")
    assert guard < text.index("deploy_sha_to_kavi \"$target_sha\"")
    assert "FORCE_DURING_DRAIN" in text
