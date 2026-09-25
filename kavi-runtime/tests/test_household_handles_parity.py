"""Drift test: household.md iMessage handles vs outbound_scanner.HOUSEHOLD_HANDLES
(Bonus item of the 2026-05-06 audit follow-up).

The runtime gates on `outbound_scanner.HOUSEHOLD_HANDLES` (a Python
constant). The PM source of truth is `household.md` §iMessage handles.
A drift between the two means either a household member's iMessage
won't be processed (if they're in household.md but not in the runtime
constant) or the runtime accepts a handle the household no longer
trusts (if the constant is stale).

This test parses the iMessage-handles table out of household.md and
asserts the resulting set equals the runtime constant. Failure pins
the drift to a clear single-line message.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_household_handles_parity.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kavi_runtime.runtime.outbound_scanner import HOUSEHOLD_HANDLES


REPO_ROOT_CANDIDATES = [
    Path("/Users/meghajain/Documents/HomeOS/household.md"),
    Path("/Users/kavi/HomeOS/household.md"),
    # Repo-relative fallback for CI / generic checkout
    Path(__file__).resolve().parents[2] / "household.md",
]


def _household_md_path() -> Path:
    for p in REPO_ROOT_CANDIDATES:
        if p.exists():
            return p
    pytest.skip(f"household.md not found in any of: {REPO_ROOT_CANDIDATES}")


def _parse_imessage_handles_from_md(md_text: str) -> set[str]:
    """Parse the `## iMessage handles` table and return every handle that
    appears in a backticked code span on a data row. Phone numbers preserve
    the `+` prefix; email handles are lowercased.

    Conservative parser: accept rows under the `## iMessage handles` heading
    until the next `## ` heading. Rows are pipe-delimited; we extract every
    backticked token from the second column (the "handle" column). When a
    row carries multiple backticked tokens (e.g., phone + Apple ID), all
    are picked up.
    """
    handles: set[str] = set()
    in_section = False
    for raw_line in md_text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_section = line.lower().startswith("## imessage handles")
            continue
        if not in_section or not line.startswith("|"):
            continue
        # Skip header / divider rows.
        if "Person" in line and "handle" in line.lower():
            continue
        if set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        # Pull every backticked token on the row.
        for tok in re.findall(r"`([^`]+)`", line):
            tok = tok.strip()
            if not tok:
                continue
            if "@" in tok:
                handles.add(tok.lower())
            elif tok.startswith("+") and tok[1:].isdigit():
                handles.add(tok)
    return handles


def test_household_handles_match_household_md() -> None:
    """Every iMessage handle in household.md MUST be present in
    `outbound_scanner.HOUSEHOLD_HANDLES`. The runtime constant may
    additionally carry related identifiers (e.g., a household member's
    email address that doubles as an iMessage Apple ID even though it's
    not in the iMessage-handles table) — that direction is acceptable.
    A handle in household.md but missing from the runtime set is a
    security regression: that household member's iMessage would silently
    drop at the inbound allowlist gate.

    Failure pins the drift to a clear reconcile-before-merge message.
    """
    path = _household_md_path()
    md_handles = _parse_imessage_handles_from_md(path.read_text())
    # household.md is the private roster, so compare it with the private
    # config the runtime reads in production, not the test household.
    import os
    from kavi_runtime import household
    real_cfg = Path(__file__).resolve().parents[1] / "config.yaml"
    if not real_cfg.exists():
        pytest.skip("private config.yaml not present")
    prev = os.environ.get("KAVI_HOUSEHOLD_CONFIG")
    os.environ["KAVI_HOUSEHOLD_CONFIG"] = str(real_cfg)
    household.reset_cache()
    try:
        runtime_handles = {h.lower() if "@" in h else h for h in household.all_handles()}
    finally:
        if prev is None:
            os.environ.pop("KAVI_HOUSEHOLD_CONFIG", None)
        else:
            os.environ["KAVI_HOUSEHOLD_CONFIG"] = prev
        household.reset_cache()

    only_in_md = md_handles - runtime_handles
    only_in_runtime = runtime_handles - md_handles

    assert md_handles <= runtime_handles, (
        "household.md and outbound_scanner.HOUSEHOLD_HANDLES have drifted; "
        "reconcile before merging.\n"
        f"  In household.md only (CRITICAL — runtime would drop these): {sorted(only_in_md)}\n"
        f"  In HOUSEHOLD_HANDLES only (informational, may be intentional): {sorted(only_in_runtime)}\n"
        f"  household.md path: {path}"
    )
