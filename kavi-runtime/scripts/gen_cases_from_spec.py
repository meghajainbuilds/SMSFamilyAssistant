#!/usr/bin/env python3
"""gen_cases_from_spec.py — turn a capability doc's Behavior section into
PROPOSED acceptance cases in the matrix schema.

Phase 2 of the doc-driven eval loop. The PM's capability doc Behavior
section (Good / Bad / Examples / Few-shot) IS the eval rubric (see
capabilities/CLAUDE.md). This script reads those PROSE examples and emits
machine-runnable matrix cases for the capability's
`POST /synthetic/verify/<cap>` route — so the doc, not a hand-maintained
matrix, is the source of the acceptance cases.

Anti-Goodhart boundary (critical): generated cases land UNFROZEN in a
staging file `evals/<cap>/matrix/proposed-cases.jsonl`. This script NEVER
writes the frozen matrix (`matrix-<cap>.jsonl`) and NEVER calls
`matrix_freeze.freeze`. The PM reviews the proposed file, edits as needed,
copies the cases she ratifies into the frozen matrix, and freezes
explicitly. Silently expanding the frozen grading set is the exact
overfitting failure the freeze ritual exists to prevent.

Why an LLM converter: Behavior examples are prose ("Inbound: 'Yes' right
after a close offer -> close that task"), not structured payloads. The
payload shape is capability-specific (the body POSTed to the verify
route). We hand the LLM (a) the ENDPOINT_CONTRACT schema for the
capability and (b) a few real rows from the existing frozen matrix as
few-shot, and ask it to convert each prose example into one
`{case_id, description, payload, expect}` row matching that schema.

Idempotence: each emitted case_id is a stable slug derived from the
example text, so re-running UPDATES a case in place rather than appending
a duplicate. The PM can re-run after editing the doc and diff the result.

Usage:
    python scripts/gen_cases_from_spec.py <capability> [--model NAME]
                                          [--max-examples N]
                                          [--evals-root PATH]
                                          [--capabilities-root PATH]

The script is import-friendly: `generate_cases(...)` takes an injectable
Anthropic-style client (anything with `.messages.create(...)`) so tests
stub it with no network call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Repo layout: this file lives at <repo>/kavi-runtime/scripts/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVALS_ROOT = _REPO_ROOT / "evals"
DEFAULT_CAPABILITIES_ROOT = _REPO_ROOT / "capabilities"

sys.path.insert(0, str(_REPO_ROOT / "kavi-runtime"))
from kavi_runtime.private_overlay import read_resolved  # noqa: E402

# Conservative default; the converter is cheap (a handful of short rows).
DEFAULT_MODEL = "claude-sonnet-4-5"
# How many real frozen rows to show the converter as the payload-shape
# few-shot. Two is enough to pin the shape without bloating the prompt.
FEWSHOT_MATRIX_ROWS = 2
# Behavior examples to convert per run. Bounded so a giant doc doesn't
# produce a 200-case proposed file the PM has to wade through.
DEFAULT_MAX_EXAMPLES = 25


# --------------------------------------------------------------------------
# Capability doc paths. The .md spec stays at the hyphen-named path for
# runtime backward compatibility (see CLAUDE.md); the eval slug matches.
# --------------------------------------------------------------------------
def capability_doc_path(capability: str, capabilities_root: Path) -> Path:
    return capabilities_root / f"{capability}.md"


def proposed_cases_path(capability: str, evals_root: Path) -> Path:
    return evals_root / capability / "matrix" / "proposed-cases.jsonl"


def endpoint_contract_path(capability: str, evals_root: Path) -> Path:
    return evals_root / capability / "matrix" / "ENDPOINT_CONTRACT.md"


# --------------------------------------------------------------------------
# Behavior-section extraction.
# --------------------------------------------------------------------------
def extract_behavior_section(doc_text: str) -> str:
    """Return the text of the `## Behavior` section (up to the next H2).
    Raises ValueError if no Behavior section is present — a doc with no
    rubric has nothing to convert."""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^##\s+Behavior\b", line):
            start = i
            break
    if start is None:
        raise ValueError(
            "no `## Behavior` section found in the capability doc — "
            "the Behavior section IS the eval rubric (capabilities/CLAUDE.md); "
            "nothing to convert."
        )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s+", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def extract_behavior_examples(behavior_section: str) -> list[str]:
    """Pull individual prose examples out of the Behavior section.

    The doc convention (capabilities/CLAUDE.md, both example docs) is:
      - `- **Examples:**` blocks whose child bullets are `Good:` / `Bad:` lines
      - `#### Few-shot N — ...` headed blocks (inbox-to-task)
      - inline `Good:` / `Bad:` example bullets under a behavior heading

    We keep the granularity coarse — one returned string per labeled
    example bullet or few-shot block — because each maps naturally to one
    acceptance case. Heading context is prefixed so the converter knows
    which behavior the example illustrates.
    """
    examples: list[str] = []
    current_heading = ""
    lines = behavior_section.splitlines()

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # Track the nearest behavior heading (### / #### / #####) for context,
        # but DON'T treat a Few-shot heading as plain context — capture it.
        m_few = re.match(r"^#{3,6}\s+Few-shot\b.*", stripped)
        if m_few:
            # A few-shot block runs until the next heading. Capture verbatim.
            block = [stripped]
            j = i + 1
            while j < len(lines) and not re.match(r"^#{3,6}\s+", lines[j].strip()):
                block.append(lines[j].rstrip())
                j += 1
            examples.append("\n".join(block).strip())
            i = j
            continue

        m_head = re.match(r"^(#{3,6})\s+(.*)", stripped)
        if m_head:
            current_heading = m_head.group(2).strip()
            i += 1
            continue

        # Example bullets: a `Good:`/`Bad:` line, or a child bullet under an
        # `Examples:` block. We capture each labeled example line as one case
        # seed, with its heading for context.
        if re.match(r"^[-*]\s+(\*\*)?(Good|Bad)\b", stripped) or re.match(
            r"^[-*]\s+.*\b(Good|Bad):", stripped
        ):
            seed = stripped.lstrip("-* ").strip()
            prefix = f"[{current_heading}] " if current_heading else ""
            examples.append(prefix + seed)
            i += 1
            continue

        i += 1

    return examples


# --------------------------------------------------------------------------
# Stable case_id slugs (idempotence).
# --------------------------------------------------------------------------
def stable_case_id(capability: str, example_text: str) -> str:
    """Deterministic slug from the example text. Re-running on an unchanged
    doc yields identical ids, so emitted rows update in place rather than
    duplicate. We slug the first ~6 informative words of the example plus a
    short content hash to disambiguate near-identical leads."""
    import hashlib

    # Drop the bracketed heading prefix and Good/Bad label for the lead words.
    lead = re.sub(r"^\[[^\]]*\]\s*", "", example_text)
    lead = re.sub(r"^\**(Good|Bad)\**:?\s*", "", lead, flags=re.IGNORECASE)
    words = re.findall(r"[a-z0-9]+", lead.lower())
    slug = "-".join(words[:6]) or "example"
    digest = hashlib.sha1(example_text.encode("utf-8")).hexdigest()[:6]
    return f"gen-{slug}-{digest}"


# --------------------------------------------------------------------------
# LLM converter.
# --------------------------------------------------------------------------
def _load_fewshot_matrix_rows(
    capability: str, evals_root: Path, n: int
) -> list[dict]:
    """Read up to `n` real rows from the frozen matrix as the payload-shape
    few-shot. Best-effort: an empty list is fine (the contract text alone
    still pins the schema)."""
    path = evals_root / capability / "matrix" / f"matrix-{capability}.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(rows) >= n:
            break
    return rows


def _load_endpoint_contract(capability: str, evals_root: Path) -> str:
    path = endpoint_contract_path(capability, evals_root)
    if path.exists():
        return path.read_text()
    return (
        f"(No ENDPOINT_CONTRACT.md found for {capability}. Infer the payload "
        f"shape from the example matrix rows below.)"
    )


def build_converter_prompt(
    capability: str,
    contract_text: str,
    fewshot_rows: list[dict],
    example_text: str,
) -> str:
    """The user message handed to the converter for ONE Behavior example.
    The contract + real rows are the schema few-shot; the example is the
    prose to convert."""
    fewshot_blob = "\n".join(json.dumps(r, ensure_ascii=False) for r in fewshot_rows)
    return f"""\
You convert ONE prose Behavior example from a HomeOS capability doc into a \
single acceptance-test case for the `POST /synthetic/verify/{capability}` \
route, in the matrix JSONL schema.

A matrix case row is a JSON object:
  {{"case_id": "<set by the caller, ignore>", "description": "<spec source + what this asserts>", "payload": {{...}}, "expect": {{"verdict": "PASS"|"FAIL", "must_contain_any"?: [..], "must_not_contain"?: [..]}}}}

The `payload` shape is capability-specific: it is the exact body POSTed to \
the verify route. Match the schema in the ENDPOINT CONTRACT and the REAL \
EXAMPLE ROWS below exactly — same keys, same value shapes.

=== ENDPOINT CONTRACT (payload schema) ===
{contract_text}

=== REAL EXAMPLE ROWS (copy this payload shape) ===
{fewshot_blob or "(none available)"}

=== THE BEHAVIOR EXAMPLE TO CONVERT ===
{example_text}

Rules:
- Construct a realistic `payload` that EXERCISES the behavior the example \
describes. A "Good:" example becomes a case that should PASS \
(verdict "PASS"); a "Bad:" example becomes a case whose payload would \
trip the gate that forbids that behavior — express it as the input that \
must NOT produce the bad output, with `must_not_contain` / forbidden \
expectations so verdict "PASS" means the bad behavior did not happen.
- Use `must_contain_any` / `must_not_contain` only on the COMPOSED OUTPUT \
text, matching how the real rows use them.
- Put the spec citation (which behavior heading) in `description`.
- Output ONLY the single JSON object. No prose, no markdown fence, no \
`case_id` invention (the caller assigns a stable id).
"""


def _client_text(response) -> str:
    """Extract the text from an Anthropic-style messages.create response.
    Tolerates both the real SDK object (`.content[i].text`) and a stub that
    returns a plain string or a simple namespace."""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if text is None and isinstance(first, dict):
            text = first.get("text")
        if text is not None:
            return text
    raise ValueError(f"could not extract text from converter response: {response!r}")


def _parse_case_json(text: str) -> dict:
    """Parse the converter's output into a dict, tolerating a stray markdown
    fence around the JSON object."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        # Grab the outermost object if there's leading/trailing prose.
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


def convert_example(
    client,
    *,
    model: str,
    capability: str,
    contract_text: str,
    fewshot_rows: list[dict],
    example_text: str,
) -> dict:
    """Convert one Behavior example into a matrix-schema row dict (with a
    stable case_id). `client` is any object with `.messages.create(...)`
    returning an Anthropic-style response."""
    prompt = build_converter_prompt(
        capability, contract_text, fewshot_rows, example_text
    )
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    case = _parse_case_json(_client_text(response))
    if not isinstance(case, dict):
        raise ValueError(f"converter returned non-object for example: {example_text!r}")
    # Caller owns the id — overwrite whatever the LLM put there with the
    # stable slug so re-runs update in place.
    case["case_id"] = stable_case_id(capability, example_text)
    case.setdefault("description", example_text)
    case.setdefault("expect", {"verdict": "PASS"})
    case.setdefault("payload", {})
    return case


# --------------------------------------------------------------------------
# Orchestration.
# --------------------------------------------------------------------------
def generate_cases(
    capability: str,
    client,
    *,
    model: str = DEFAULT_MODEL,
    evals_root: Path = DEFAULT_EVALS_ROOT,
    capabilities_root: Path = DEFAULT_CAPABILITIES_ROOT,
    max_examples: int = DEFAULT_MAX_EXAMPLES,
) -> list[dict]:
    """Read the capability's Behavior section, convert each example via the
    injected client, and WRITE the proposed (unfrozen) cases file. Returns
    the list of case rows written. Never touches the frozen matrix."""
    doc_path = capability_doc_path(capability, capabilities_root)
    if not doc_path.exists():
        raise FileNotFoundError(f"capability doc missing: {doc_path}")

    behavior = extract_behavior_section(read_resolved(doc_path))
    examples = extract_behavior_examples(behavior)
    if not examples:
        raise ValueError(
            f"no Behavior examples found in {doc_path} — nothing to convert."
        )
    examples = examples[:max_examples]

    contract_text = _load_endpoint_contract(capability, evals_root)
    fewshot_rows = _load_fewshot_matrix_rows(capability, evals_root, FEWSHOT_MATRIX_ROWS)

    cases: list[dict] = []
    seen_ids: set[str] = set()
    for ex in examples:
        case = convert_example(
            client,
            model=model,
            capability=capability,
            contract_text=contract_text,
            fewshot_rows=fewshot_rows,
            example_text=ex,
        )
        cid = case["case_id"]
        if cid in seen_ids:
            # Two near-identical example bullets slugged the same id: the
            # later one wins (idempotent update), no duplicate row.
            cases = [c for c in cases if c["case_id"] != cid]
        seen_ids.add(cid)
        cases.append(case)

    out_path = proposed_cases_path(capability, evals_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n")
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capability")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-examples", type=int, default=DEFAULT_MAX_EXAMPLES)
    parser.add_argument("--evals-root", default=str(DEFAULT_EVALS_ROOT))
    parser.add_argument("--capabilities-root", default=str(DEFAULT_CAPABILITIES_ROOT))
    args = parser.parse_args(argv)

    try:
        import anthropic
    except ImportError:
        print(
            "[gen-cases] FAIL: the anthropic SDK is not installed in this "
            "environment. Install it or call generate_cases(...) with an "
            "injected client.",
            file=sys.stderr,
        )
        return 1
    client = anthropic.Anthropic()

    try:
        cases = generate_cases(
            args.capability,
            client,
            model=args.model,
            evals_root=Path(args.evals_root),
            capabilities_root=Path(args.capabilities_root),
            max_examples=args.max_examples,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"[gen-cases] FAIL: {e}", file=sys.stderr)
        return 1

    out_path = proposed_cases_path(args.capability, Path(args.evals_root))
    print(
        f"[gen-cases] wrote {len(cases)} PROPOSED cases for "
        f"{args.capability!r} -> {out_path}"
    )
    print(
        "[gen-cases] These are NOT frozen. Review them, edit as needed, then\n"
        "            copy the cases you ratify into "
        f"matrix-{args.capability}.jsonl and run:\n"
        f"            python scripts/matrix_freeze.py freeze {args.capability}\n"
        "            Nothing grades against these until you do."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
