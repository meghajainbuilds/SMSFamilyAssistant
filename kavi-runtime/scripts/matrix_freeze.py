#!/usr/bin/env python3
"""matrix_freeze.py — freeze + tamper-check the per-capability test matrix.

Part of the capability-build pipeline (capabilities/BUILD_PIPELINE.md).
Anti-Goodhart property #1: the test matrix is FROZEN (hash-stamped) before
fix iteration begins. The fix author cannot edit the matrix mid-iteration
without `check` failing loudly — drift between the matrix file and its
manifest is treated as tampering, and the runner (scripts/run_matrix.py)
refuses to run until the matrix is re-frozen (a deliberate, visible act).

Matrix file format (JSONL, one case per line):

    {
      "case_id": "morning-all-clear",
      "description": "spec source + what this case asserts",
      "payload": { ...exact /synthetic/verify/<capability> body... },
      "expect": {
        "verdict": "PASS" | "FAIL",
        "must_contain_any": ["substr", ...],   # optional, on composed output
        "must_not_contain": ["substr", ...]    # optional, on composed output
      }
    }

Layout per capability slug:

    evals/<slug>/matrix/matrix-<slug>.jsonl            # the frozen matrix
    evals/<slug>/matrix/matrix-<slug>.manifest.json    # freeze manifest
    evals/<slug>/matrix/iteration_count.json           # runner bound counter
    evals/<slug>/matrix/runs/run-<ts>.jsonl            # runner logs

Usage:
    python scripts/matrix_freeze.py freeze <capability> [--timestamp ISO]
    python scripts/matrix_freeze.py check  <capability>

`freeze` computes sha256 over the canonicalized JSONL (each row parsed and
re-dumped with sorted keys — whitespace-insensitive, content-sensitive),
writes the manifest, and RESETS the iteration counter to zero.

`check` exits nonzero with a loud message when the current matrix content
hash differs from the manifest (tamper / drift detection), when the
manifest is missing, or when the matrix file is missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Repo layout: this file lives at <repo>/kavi-runtime/scripts/, the eval
# surfaces live at <repo>/evals/. Overridable for tests via --evals-root /
# the evals_root argument.
DEFAULT_EVALS_ROOT = Path(__file__).resolve().parents[2] / "evals"

EXPECTED_VERDICTS = {"PASS", "FAIL"}
REQUIRED_ROW_KEYS = {"case_id", "description", "payload", "expect"}


def matrix_path(capability: str, evals_root: Path) -> Path:
    return evals_root / capability / "matrix" / f"matrix-{capability}.jsonl"


def manifest_path(capability: str, evals_root: Path) -> Path:
    return evals_root / capability / "matrix" / f"matrix-{capability}.manifest.json"


def iteration_count_path(capability: str, evals_root: Path) -> Path:
    return evals_root / capability / "matrix" / "iteration_count.json"


def load_matrix_rows(capability: str, evals_root: Path) -> list[dict]:
    """Parse the matrix JSONL. Raises ValueError with a per-line message on
    malformed JSON or a row that misses required keys."""
    path = matrix_path(capability, evals_root)
    if not path.exists():
        raise FileNotFoundError(f"matrix file missing: {path}")
    rows: list[dict] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: not valid JSON: {e}") from e
        missing = REQUIRED_ROW_KEYS - set(row)
        if missing:
            raise ValueError(
                f"{path}:{lineno}: row missing required keys: {sorted(missing)}"
            )
        verdict = (row.get("expect") or {}).get("verdict")
        if verdict not in EXPECTED_VERDICTS:
            raise ValueError(
                f"{path}:{lineno}: expect.verdict must be one of "
                f"{sorted(EXPECTED_VERDICTS)}, got {verdict!r}"
            )
        if not isinstance(row.get("payload"), dict):
            raise ValueError(f"{path}:{lineno}: payload must be a JSON object")
        case_id = row["case_id"]
        if case_id in seen_ids:
            raise ValueError(f"{path}:{lineno}: duplicate case_id {case_id!r}")
        seen_ids.add(case_id)
        rows.append(row)
    if not rows:
        raise ValueError(f"{path}: matrix is empty (no cases)")
    return rows


def canonicalized_text(rows: list[dict]) -> str:
    """Whitespace-insensitive, content-sensitive canonical form: each row
    dumped with sorted keys + tight separators, newline-joined."""
    return (
        "\n".join(
            json.dumps(r, sort_keys=True, separators=(",", ":")) for r in rows
        )
        + "\n"
    )


def compute_matrix_sha256(capability: str, evals_root: Path) -> tuple[str, int]:
    """Returns (sha256 hex digest of the canonicalized matrix, case count)."""
    rows = load_matrix_rows(capability, evals_root)
    digest = hashlib.sha256(canonicalized_text(rows).encode("utf-8")).hexdigest()
    return digest, len(rows)


def freeze(
    capability: str,
    evals_root: Path = DEFAULT_EVALS_ROOT,
    timestamp: str | None = None,
) -> dict:
    """Hash-stamp the matrix and reset the iteration counter. Returns the
    manifest dict that was written."""
    sha, case_count = compute_matrix_sha256(capability, evals_root)
    if timestamp is None:
        mtime = matrix_path(capability, evals_root).stat().st_mtime
        timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    manifest = {
        "capability": capability,
        "frozen_at": timestamp,
        "sha256": sha,
        "case_count": case_count,
    }
    mpath = manifest_path(capability, evals_root)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2) + "\n")

    # Anti-Goodhart property #2 hook: freezing resets the bounded-iteration
    # counter. The runner increments it per invocation; past the bound it
    # exits nonzero with a stop-and-surface banner.
    counter = {
        "capability": capability,
        "frozen_sha256": sha,
        "frozen_at": timestamp,
        "runs_since_freeze": 0,
    }
    iteration_count_path(capability, evals_root).write_text(
        json.dumps(counter, indent=2) + "\n"
    )
    return manifest


def check(capability: str, evals_root: Path = DEFAULT_EVALS_ROOT) -> tuple[bool, str]:
    """Compare the current matrix content hash against the manifest.
    Returns (ok, message)."""
    mpath = manifest_path(capability, evals_root)
    if not mpath.exists():
        return False, (
            f"NO MANIFEST for capability {capability!r} at {mpath}. "
            f"The matrix was never frozen. Run: matrix_freeze.py freeze {capability}"
        )
    try:
        manifest = json.loads(mpath.read_text())
    except json.JSONDecodeError as e:
        return False, f"manifest at {mpath} is not valid JSON: {e}"
    try:
        current_sha, case_count = compute_matrix_sha256(capability, evals_root)
    except (FileNotFoundError, ValueError) as e:
        return False, f"matrix unreadable for capability {capability!r}: {e}"
    frozen_sha = manifest.get("sha256")
    if current_sha != frozen_sha:
        return False, (
            "=" * 72 + "\n"
            f"MATRIX TAMPER / DRIFT DETECTED for capability {capability!r}.\n"
            f"  frozen sha256 : {frozen_sha}\n"
            f"  current sha256: {current_sha}\n"
            f"The matrix file changed AFTER it was frozen. The fix author must\n"
            f"not edit the matrix during fix iteration (anti-Goodhart rule #1,\n"
            f"see capabilities/BUILD_PIPELINE.md). If the matrix change is\n"
            f"legitimate (new spec example, PM-approved), re-freeze explicitly:\n"
            f"  python scripts/matrix_freeze.py freeze {capability}\n"
            f"and say so in chat — re-freezing also resets the iteration bound.\n"
            + "=" * 72
        )
    return True, (
        f"matrix OK for {capability!r}: sha256 matches manifest "
        f"({case_count} cases, frozen_at={manifest.get('frozen_at')})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_freeze = sub.add_parser("freeze", help="hash-stamp the matrix + reset iteration counter")
    p_freeze.add_argument("capability")
    p_freeze.add_argument("--timestamp", default=None,
                          help="ISO timestamp for frozen_at (default: matrix file mtime)")
    p_freeze.add_argument("--evals-root", default=str(DEFAULT_EVALS_ROOT))

    p_check = sub.add_parser("check", help="verify the matrix still matches its manifest")
    p_check.add_argument("capability")
    p_check.add_argument("--evals-root", default=str(DEFAULT_EVALS_ROOT))

    args = parser.parse_args(argv)
    evals_root = Path(args.evals_root)

    if args.command == "freeze":
        try:
            manifest = freeze(args.capability, evals_root, timestamp=args.timestamp)
        except (FileNotFoundError, ValueError) as e:
            print(f"[matrix-freeze] FAIL: {e}", file=sys.stderr)
            return 1
        print(
            f"[matrix-freeze] frozen {args.capability}: "
            f"sha256={manifest['sha256'][:16]}… cases={manifest['case_count']} "
            f"frozen_at={manifest['frozen_at']} (iteration counter reset to 0)"
        )
        return 0

    ok, message = check(args.capability, evals_root)
    stream = sys.stdout if ok else sys.stderr
    print(f"[matrix-freeze] {message}", file=stream)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
