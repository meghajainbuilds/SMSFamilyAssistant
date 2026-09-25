#!/usr/bin/env python3
"""label_writeback.py — the gradient: the PM's judgment on an eval output
flows BACK into the capability doc and the proposed-cases file.

Phase 2 of the doc-driven eval loop, the closing half. `gen_cases_from_spec`
turns the doc into cases (doc -> cases). This turns a label into a new rule
(label -> doc + cases). When Megha looks at an eval output and says "this is
wrong, it should have named the fact's topic," that one-line correction:

  (a) appends a new few-shot example (Good or Bad) to the capability doc's
      Behavior section, reflecting the correction — so the rubric itself
      grows, and the next `gen_cases_from_spec` run picks it up; and
  (b) appends a corresponding PROPOSED case to
      `evals/<cap>/matrix/proposed-cases.jsonl` — so there is a runnable
      acceptance case staged for the PM to ratify.

Her label raises the bar. The doc edit is the durable artifact; the
proposed case is the staged test.

Anti-Goodhart boundary (same as gen_cases_from_spec): this writes the
UNFROZEN proposed-cases file and the doc. It NEVER edits the frozen matrix
(`matrix-<cap>.jsonl`) and NEVER freezes. The PM ratifies and freezes
explicitly.

CLI:
    python scripts/label_writeback.py <cap> --case <id> --label wrong \\
        --correction "should name the fact's topic"
    python scripts/label_writeback.py <cap> --case <id> --label right \\
        --correction "delivered the teacher-meeting fact cleanly"

`--label wrong` appends a Bad example (the behavior to avoid, with the
correction as the Good counterpart). `--label right` appends a Good example
(reinforcing the behavior that worked).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

# Reuse the path + slug helpers so the two scripts agree on layout.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_cases_from_spec as gcs  # noqa: E402

DEFAULT_EVALS_ROOT = gcs.DEFAULT_EVALS_ROOT
DEFAULT_CAPABILITIES_ROOT = gcs.DEFAULT_CAPABILITIES_ROOT

# Where in the Behavior section the learned examples accrue. We keep a
# single dedicated subsection so writebacks are easy to find, review, and
# (later) promote into the hand-curated example blocks. One canonical home.
LEARNED_HEADING = "#### Learned from labels (writeback)"


def _label_to_kind(label: str) -> str:
    norm = label.strip().lower()
    if norm in {"wrong", "bad", "fail", "incorrect"}:
        return "Bad"
    if norm in {"right", "good", "pass", "correct"}:
        return "Good"
    raise ValueError(
        f"unrecognized --label {label!r}; use 'wrong'/'bad' or 'right'/'good'."
    )


def build_doc_example_line(
    *, kind: str, case_id_or_output: str, correction: str, on: date
) -> str:
    """One Behavior-section bullet matching the doc's existing example
    formatting (`- **Bad:** ...` / `- **Good:** ...`). The correction is the
    PM's one-liner; the case/output reference and date give provenance."""
    ref = case_id_or_output.strip()
    corr = correction.strip().rstrip(".")
    if kind == "Bad":
        body = (
            f"**Bad:** {corr} — flagged wrong on `{ref}` ({on.isoformat()}). "
            f"The correction is the Good behavior to match."
        )
    else:
        body = (
            f"**Good:** {corr} — confirmed right on `{ref}` ({on.isoformat()})."
        )
    return f"- {body}"


def _privatize_if_needed(
    bullet: str, capability: str, *, kind: str, on: date, capabilities_root: Path
) -> str:
    """Public-repo rule: a learned example naming a family detail (any term
    in private/denylist.txt) goes to the private overlay, and the public doc
    gets a private block with a generic stand-in. Resolved on Kavi, the
    doc reads exactly as if the bullet had been written in place."""
    import hashlib
    root = capabilities_root.resolve().parent
    deny = root / "private" / "denylist.txt"
    if not deny.exists():
        return bullet
    terms = [t.strip() for t in deny.read_text().splitlines()
             if t.strip() and not t.startswith("#")]
    if not any(re.search(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", bullet, re.IGNORECASE)
               for t in terms):
        return bullet
    bid = f"learned-{capability}-{on.isoformat()}-{hashlib.sha1(bullet.encode()).hexdigest()[:8]}"
    overlay_file = root / "private" / "overlays" / f"capabilities__{capability}.json"
    overlay_file.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(overlay_file.read_text()) if overlay_file.exists() else {}
    data[bid] = bullet
    overlay_file.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return (f"<!-- private:{bid} -->- **{kind}:** family-specific example, "
            f"kept private ({on.isoformat()}).<!-- /private -->")


def append_doc_example(
    capability: str,
    *,
    kind: str,
    case_id_or_output: str,
    correction: str,
    capabilities_root: Path,
    on: date | None = None,
) -> str:
    """Surgically insert a learned example under the Behavior section.

    Strategy: find `## Behavior`. Ensure a `#### Learned from labels
    (writeback)` subsection exists at the END of the Behavior section
    (inserted just before the next `## ` heading, or appended if Behavior
    is the last section). Append the new bullet under it. Returns the bullet
    that was written. Never reorders or rewrites existing content.
    """
    on = on or date.today()
    doc_path = gcs.capability_doc_path(capability, capabilities_root)
    if not doc_path.exists():
        raise FileNotFoundError(f"capability doc missing: {doc_path}")

    text = doc_path.read_text()
    lines = text.splitlines()

    # Locate the Behavior section span.
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^##\s+Behavior\b", line):
            start = i
            break
    if start is None:
        raise ValueError(
            f"no `## Behavior` section in {doc_path}; cannot write back a label."
        )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s+", lines[j]):
            end = j
            break

    bullet = build_doc_example_line(
        kind=kind, case_id_or_output=case_id_or_output, correction=correction, on=on
    )
    bullet = _privatize_if_needed(bullet, capability, kind=kind, on=on,
                                  capabilities_root=capabilities_root)

    # Is the learned subsection already present within Behavior?
    learned_idx = None
    for k in range(start, end):
        if lines[k].strip() == LEARNED_HEADING:
            learned_idx = k
            break

    if learned_idx is not None:
        # Append the bullet at the end of the learned subsection (which runs
        # until the next heading of any level, or the Behavior section end).
        insert_at = end
        for k in range(learned_idx + 1, end):
            if re.match(r"^#{1,6}\s+", lines[k]):
                insert_at = k
                break
        # Trim a trailing blank inside the subsection so bullets stay tight.
        while insert_at - 1 > learned_idx and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        new_lines = lines[:insert_at] + [bullet] + lines[insert_at:]
    else:
        # Create the subsection at the end of the Behavior section.
        block = ["", LEARNED_HEADING, "", bullet]
        # Drop any trailing blank lines right at the Behavior boundary so we
        # don't stack blanks.
        boundary = end
        while boundary - 1 > start and lines[boundary - 1].strip() == "":
            boundary -= 1
        new_lines = lines[:boundary] + block + lines[boundary:]

    doc_path.write_text("\n".join(new_lines) + ("\n" if text.endswith("\n") else ""))
    return bullet


def build_proposed_case(
    capability: str,
    *,
    kind: str,
    case_id_or_output: str,
    correction: str,
    on: date | None = None,
) -> dict:
    """A matrix-schema PROPOSED case carrying the correction. The payload is
    a placeholder the PM fleshes out at ratify time — the value here is the
    description (spec provenance) and the expectation derived from the
    correction. We do NOT fabricate a full capability-specific payload from a
    one-line label; an honest stub the PM completes beats a guessed payload
    that looks runnable but isn't grounded."""
    on = on or date.today()
    base_ref = re.sub(r"[^a-z0-9]+", "-", case_id_or_output.strip().lower()).strip("-")
    base_ref = base_ref or "label"
    case_id = f"label-{base_ref}-{on.isoformat().replace('-', '')}"
    expect: dict = {"verdict": "PASS"}
    corr = correction.strip()
    if kind == "Bad":
        # The corrected behavior should NOT exhibit the flagged failure; the
        # PM tightens must_not_contain at ratify time. Empty list is a valid,
        # honest placeholder (no phrase guessed from a one-liner).
        expect["must_not_contain"] = []
    return {
        "case_id": case_id,
        "description": (
            f"Writeback from PM label ({kind}) on `{case_id_or_output}` "
            f"({on.isoformat()}): {corr}. PROPOSED — complete the payload + "
            f"expectations, then ratify into the frozen matrix."
        ),
        "payload": {"_writeback_stub": True, "correction": corr},
        "expect": expect,
    }


def append_proposed_case(
    capability: str, case: dict, evals_root: Path
) -> Path:
    """Append the proposed case to the unfrozen proposed-cases file
    (idempotent on case_id: a re-labeled case with the same id replaces the
    prior row rather than duplicating). Returns the file path."""
    path = gcs.proposed_cases_path(capability, evals_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    existing = [c for c in existing if c.get("case_id") != case["case_id"]]
    existing.append(case)
    path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in existing) + "\n"
    )
    return path


def record_label(
    capability: str,
    case_id_or_output: str,
    verdict_label: str,
    correction: str,
    evals_root: Path = DEFAULT_EVALS_ROOT,
    capabilities_root: Path = DEFAULT_CAPABILITIES_ROOT,
    on: date | None = None,
) -> dict:
    """The public entry point. Given the PM's judgment on an eval output
    (right/wrong + a one-line correction), (a) append a Good/Bad example to
    the capability doc Behavior section and (b) append a proposed case to
    proposed-cases.jsonl. Never edits the frozen matrix.

    Returns a summary: {kind, doc_bullet, doc_path, case, proposed_path}.
    """
    kind = _label_to_kind(verdict_label)
    on = on or date.today()

    doc_bullet = append_doc_example(
        capability,
        kind=kind,
        case_id_or_output=case_id_or_output,
        correction=correction,
        capabilities_root=capabilities_root,
        on=on,
    )
    case = build_proposed_case(
        capability,
        kind=kind,
        case_id_or_output=case_id_or_output,
        correction=correction,
        on=on,
    )
    proposed_path = append_proposed_case(capability, case, evals_root)

    return {
        "kind": kind,
        "doc_bullet": doc_bullet,
        "doc_path": str(gcs.capability_doc_path(capability, capabilities_root)),
        "case": case,
        "proposed_path": str(proposed_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capability")
    parser.add_argument(
        "--case",
        required=True,
        dest="case_id_or_output",
        help="the eval case_id (or a short output reference) being labeled",
    )
    parser.add_argument(
        "--label",
        required=True,
        help="'wrong'/'bad' (appends a Bad example) or 'right'/'good' "
        "(appends a Good example)",
    )
    parser.add_argument(
        "--correction",
        required=True,
        help="one-line correction / what good looks like",
    )
    parser.add_argument("--evals-root", default=str(DEFAULT_EVALS_ROOT))
    parser.add_argument("--capabilities-root", default=str(DEFAULT_CAPABILITIES_ROOT))
    args = parser.parse_args(argv)

    try:
        result = record_label(
            args.capability,
            args.case_id_or_output,
            args.label,
            args.correction,
            evals_root=Path(args.evals_root),
            capabilities_root=Path(args.capabilities_root),
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"[label-writeback] FAIL: {e}", file=sys.stderr)
        return 1

    print(
        f"[label-writeback] {result['kind']} example appended to "
        f"{result['doc_path']} Behavior section:\n    {result['doc_bullet']}"
    )
    print(
        f"[label-writeback] proposed case {result['case']['case_id']!r} appended to "
        f"{result['proposed_path']} (NOT frozen)."
    )
    print(
        "[label-writeback] Complete the payload + expectations on that case, "
        "then copy it into\n"
        f"            matrix-{args.capability}.jsonl and run "
        f"`matrix_freeze.py freeze {args.capability}` to grade against it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
