"""Pattern audit script.

Greps the kavi-runtime codebase for known anti-patterns named in past
post-mortems (engineering-discipline.md §3 — "pattern audit on naming").
Writes a markdown report to stdout. Each match is a clickable
file:line reference reviewers can jump to.

Anti-patterns checked (each is a known bug family, not a single
instance):
  1. Slice caps before LLM calls — `[:N]` immediately upstream of a
     classify_/compose_/match_target_to_open_task call. Deterministic
     narrowing before LLM judgment was the 2026-05-07 Elders' Tea bug
     class.
  2. Regex filters between LLM calls — `re.search` / `re.match` /
     `re.findall` / `re.sub` in the same function as 2+ LLM calls.
     "Bug factory" per the LLM-first-action-layer principle.
  3. Hardcoded numeric timeouts / max_tokens — literal int values on
     `max_tokens=` and `timeout=` outside named constants. The
     2026-05-08 clip bug had max_tokens=400 buried inline.
  4. Hardcoded Graph paging caps — `$top=N` literals in URL strings;
     should be named constants for review.
  5. Bare except — `except:` or `except Exception: pass` patterns.
     Skipped error handling masks failures (see post-mortem miss #6).

Usage:
    python kavi-runtime/scripts/pattern_audit.py [--root <runtime_dir>]

Exits 0 even if findings exist; the report is informational. To gate
on findings, parse stdout or grep for "## " sections.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Match:
    file: str
    line: int
    text: str
    detail: str = ""

    def render_md(self) -> str:
        head = f"- `{self.file}:{self.line}` — `{self.text.strip()[:120]}`"
        if self.detail:
            head += f"  \n  {self.detail}"
        return head


# ---------- Discovery -------------------------------------------------------

LLM_CALL_PATTERN = re.compile(
    r"\b(?:claude|claude_client|claude_c|self|client|self\._anthropic|client\._anthropic|anthropic)\.\s*"
    r"(?:messages\.create|_anthropic\.messages\.create|classify_\w+|compose_\w+|match_target_to_open_task|"
    r"resolve_pending_action_clarification|check_semantic_duplicate)"
)

# Slice caps directly upstream of the LLM call: `[:N]` literal that ends a
# subscription on a name (foo[:30]) — generic; we filter further by
# colocation in same function as an LLM call.
SLICE_LITERAL = re.compile(r"\[\s*:\s*(\d+)\s*\]")

# `re.<fn>(` calls.
REGEX_CALL = re.compile(r"\bre\.(search|match|findall|sub|finditer)\s*\(")

# Hardcoded timeouts / max_tokens literals.
MAX_TOKENS_LITERAL = re.compile(r"max_tokens\s*=\s*(\d+)\b")
TIMEOUT_LITERAL = re.compile(r"\btimeout\s*=\s*([0-9]+(?:\.[0-9]+)?)\b")

# Graph paging caps inside URL strings.
DOLLAR_TOP_LITERAL = re.compile(r"\$top\s*=\s*(\d+)")
DOLLAR_TOP_PARAM = re.compile(r"['\"]\$top['\"]\s*:\s*(\d+)")

# Bare except.
BARE_EXCEPT = re.compile(r"^\s*except\s*:\s*$")
EXCEPT_PASS = re.compile(r"^\s*except\b[^:]*:\s*pass\s*(#.*)?$")


def _python_files(root: Path) -> Iterator[Path]:
    """Yield every .py file under root, skipping virtualenvs, caches, and tests/."""
    skip_dirs = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    for p in root.rglob("*.py"):
        # Don't audit tests or scripts directories — those are
        # intentionally test-shaped and the patterns there are not the
        # production bug families this script targets.
        parts = set(p.parts)
        if parts & skip_dirs:
            continue
        if "tests" in parts or "scripts" in parts:
            continue
        yield p


def _enumerate_lines(path: Path) -> Iterator[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    for i, line in enumerate(text.splitlines(), start=1):
        yield i, line


def _function_for_line(file_lines: list[str], line_idx: int) -> str | None:
    """Walk backwards from `line_idx` to find the enclosing `def`."""
    pat = re.compile(r"^\s*def\s+(\w+)\s*\(")
    for i in range(line_idx - 1, -1, -1):
        m = pat.match(file_lines[i])
        if m:
            return m.group(1)
    return None


# ---------- Pattern checks --------------------------------------------------


def find_slices_upstream_of_llm_calls(root: Path) -> list[Match]:
    """Slice caps in the same function as an LLM call. Reports each
    slice + the function it sits in, with the LLM call line for
    context."""
    out: list[Match] = []
    for path in _python_files(root):
        lines = path.read_text(encoding="utf-8").splitlines()
        # Pre-scan: which function does each line belong to + does that
        # function contain an LLM call?
        fn_for_line: dict[int, str | None] = {}
        functions_with_llm_calls: set[str] = set()
        current_fn: str | None = None
        def_pat = re.compile(r"^(\s*)def\s+(\w+)\s*\(")
        def_indent: int = -1
        for i, line in enumerate(lines):
            m = def_pat.match(line)
            if m:
                indent = len(m.group(1))
                # Pop back to a top-level if dedent.
                if def_indent == -1 or indent <= def_indent:
                    current_fn = m.group(2)
                    def_indent = indent
            fn_for_line[i] = current_fn
            if current_fn and LLM_CALL_PATTERN.search(line):
                functions_with_llm_calls.add(current_fn)

        # Now flag slices in flagged functions.
        for i, line in enumerate(lines):
            fn = fn_for_line.get(i)
            if not fn or fn not in functions_with_llm_calls:
                continue
            m = SLICE_LITERAL.search(line)
            if not m:
                continue
            # Skip type annotations like `list[:30]` are unlikely; this
            # regex catches both. We surface them all and let the
            # reviewer judge.
            out.append(Match(
                file=str(path.relative_to(root)),
                line=i + 1,
                text=line,
                detail=f"slice `[:{m.group(1)}]` inside fn `{fn}` (function calls an LLM upstream/downstream)",
            ))
    return out


def find_regex_calls_with_multiple_llm_calls(root: Path) -> list[Match]:
    """re.* calls inside a function that has 2+ LLM calls. The
    LLM-first principle says deterministic regex steps between LLM
    calls are bug factories."""
    out: list[Match] = []
    for path in _python_files(root):
        lines = path.read_text(encoding="utf-8").splitlines()
        # First pass: count LLM calls per function.
        current_fn: str | None = None
        def_pat = re.compile(r"^(\s*)def\s+(\w+)\s*\(")
        def_indent: int = -1
        llm_count_per_fn: dict[str, int] = {}
        regex_lines_per_fn: dict[str, list[tuple[int, str]]] = {}
        for i, line in enumerate(lines):
            m = def_pat.match(line)
            if m:
                indent = len(m.group(1))
                if def_indent == -1 or indent <= def_indent:
                    current_fn = m.group(2)
                    def_indent = indent
                    llm_count_per_fn.setdefault(current_fn, 0)
                    regex_lines_per_fn.setdefault(current_fn, [])
            if not current_fn:
                continue
            if LLM_CALL_PATTERN.search(line):
                llm_count_per_fn[current_fn] = llm_count_per_fn.get(current_fn, 0) + 1
            if REGEX_CALL.search(line):
                regex_lines_per_fn.setdefault(current_fn, []).append((i + 1, line))

        for fn, regex_lines in regex_lines_per_fn.items():
            if llm_count_per_fn.get(fn, 0) < 2:
                continue
            for ln, line in regex_lines:
                out.append(Match(
                    file=str(path.relative_to(root)),
                    line=ln,
                    text=line,
                    detail=(
                        f"re.* call inside fn `{fn}` which has "
                        f"{llm_count_per_fn[fn]} LLM calls — review for "
                        f"deterministic-narrowing-between-LLM-calls"
                    ),
                ))
    return out


def find_hardcoded_max_tokens(root: Path) -> list[Match]:
    """`max_tokens=<int>` literal."""
    out: list[Match] = []
    for path in _python_files(root):
        for i, line in _enumerate_lines(path):
            m = MAX_TOKENS_LITERAL.search(line)
            if m:
                out.append(Match(
                    file=str(path.relative_to(root)),
                    line=i,
                    text=line,
                    detail=f"hardcoded max_tokens={m.group(1)}",
                ))
    return out


def find_hardcoded_timeouts(root: Path) -> list[Match]:
    """`timeout=<int>` literal. Excludes `timeout=None` and module-level
    constants (we scan for literal numbers only)."""
    out: list[Match] = []
    for path in _python_files(root):
        for i, line in _enumerate_lines(path):
            m = TIMEOUT_LITERAL.search(line)
            if m:
                out.append(Match(
                    file=str(path.relative_to(root)),
                    line=i,
                    text=line,
                    detail=f"hardcoded timeout={m.group(1)}",
                ))
    return out


def find_hardcoded_graph_top(root: Path) -> list[Match]:
    """`$top=<int>` in URL strings or `'$top': <int>` in params dicts."""
    out: list[Match] = []
    for path in _python_files(root):
        for i, line in _enumerate_lines(path):
            for pat in (DOLLAR_TOP_LITERAL, DOLLAR_TOP_PARAM):
                m = pat.search(line)
                if m:
                    out.append(Match(
                        file=str(path.relative_to(root)),
                        line=i,
                        text=line,
                        detail=f"hardcoded $top={m.group(1)} (Graph paging cap)",
                    ))
                    break
    return out


def find_bare_except(root: Path) -> list[Match]:
    """`except:` or `except <X>: pass` patterns — skipped error handling."""
    out: list[Match] = []
    for path in _python_files(root):
        for i, line in _enumerate_lines(path):
            if BARE_EXCEPT.match(line) or EXCEPT_PASS.match(line):
                out.append(Match(
                    file=str(path.relative_to(root)),
                    line=i,
                    text=line,
                    detail="bare except / except-pass — review whether the failure is intentionally swallowed",
                ))
    return out


# ---------- Render ----------------------------------------------------------


def render_section(title: str, summary: str, matches: list[Match]) -> str:
    body = [f"## {title}", "", summary, ""]
    if not matches:
        body.append("_No matches found._")
        body.append("")
        return "\n".join(body)
    body.append(f"**{len(matches)} match(es):**")
    body.append("")
    for m in matches:
        body.append(m.render_md())
    body.append("")
    return "\n".join(body)


def render_report(
    root: Path,
    findings: dict[str, tuple[str, list[Match]]],
) -> str:
    """findings: dict {section_title: (summary, matches)}."""
    lines = [
        "# Pattern audit report",
        "",
        f"Audited tree: `{root}`",
        f"Sections: {len(findings)}",
        "",
    ]
    for title, (summary, matches) in findings.items():
        lines.append(render_section(title, summary, matches))
    return "\n".join(lines)


# ---------- Main ------------------------------------------------------------


def run_audit(root: Path) -> str:
    findings: dict[str, tuple[str, list[Match]]] = {}

    findings["Slice caps before LLM calls"] = (
        "`[:N]` slice literals inside the same function as an LLM call. "
        "Deterministic narrowing before LLM judgment is a known bug "
        "factory (2026-05-07 Elders' Tea, 2026-05-08 [:30] matcher cap). "
        "Each match should be reviewed for whether the LLM could do the "
        "narrowing itself.",
        find_slices_upstream_of_llm_calls(root),
    )
    findings["Regex filters between LLM calls"] = (
        "`re.*` calls inside a function that contains 2+ LLM calls. "
        "Architecture principle: any deterministic string-matcher "
        "between LLM steps is a bug factory.",
        find_regex_calls_with_multiple_llm_calls(root),
    )
    findings["Hardcoded max_tokens literals"] = (
        "`max_tokens=<int>` literals in code. Should be named constants "
        "or config values so prompt-cap drift can be reviewed in one "
        "place. The 2026-05-08 clip bug was max_tokens=400 buried inline.",
        find_hardcoded_max_tokens(root),
    )
    findings["Hardcoded timeout literals"] = (
        "`timeout=<int>` literals in code. Same review bar as max_tokens — "
        "named constants help when tuning under load.",
        find_hardcoded_timeouts(root),
    )
    findings["Hardcoded Graph paging caps"] = (
        "`$top=<int>` in URL strings or params. Tonight's recency-only "
        "top=30 fetch dropped older open tasks; the cap should be a "
        "named constant or config value, not a string literal.",
        find_hardcoded_graph_top(root),
    )
    findings["Bare except / except-pass"] = (
        "`except:` and `except <X>: pass` patterns. Swallowed errors "
        "mask failures; the discipline doc post-mortem (miss #6) calls "
        "this out as a recurring blind spot.",
        find_bare_except(root),
    )
    return render_report(root, findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit kavi-runtime for known anti-patterns.")
    default_root = Path(__file__).resolve().parents[1] / "kavi_runtime"
    parser.add_argument("--root", type=Path, default=default_root,
                        help=f"audit root (default: {default_root})")
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"audit root does not exist: {args.root}", file=sys.stderr)
        return 2

    report = run_audit(args.root)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
