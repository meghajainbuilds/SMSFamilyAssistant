#!/usr/bin/env python3
"""Leak guard (public-repo step 7). Fails if tracked or staged text contains
a private term or a secret-shaped string.

Private terms come from private/denylist.txt (gitignored; absent on public
clones, where only the generic patterns run). Text inside
`<!-- private:ID -->...<!-- /private -->` blocks is public stand-in text and
is scanned like everything else.

    python3 scripts/leak_scan.py --staged     # pre-commit hook
    python3 scripts/leak_scan.py --all        # whole tracked tree
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = ROOT / "private" / "denylist.txt"
# Files whose secret-shaped strings are deliberate fakes (redaction tests).
ALLOW_FILES = {"scripts/leak_scan.py", "kavi-runtime/tests/test_extended_sensitive_patterns.py"}
GENERIC = [
    ("graph id", r"AQMk[A-Za-z0-9_=-]{40,}"),
    ("healthchecks url", r"hc-ping\.com/[0-9a-f-]{20,}"),
    ("tailnet hostname", r"[a-z0-9-]+\.tail[a-z0-9]{4,}\.ts\.net"),
    ("anthropic key", r"sk-ant-(?!X{6}|test)[A-Za-z0-9_-]{10,}"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("us phone", r"\+1(?!555|999)\d{10}"),
]


def _terms() -> list[str]:
    if not DENYLIST.exists():
        return []
    return [t.strip() for t in DENYLIST.read_text().splitlines() if t.strip() and not t.startswith("#")]


def _files(staged: bool) -> list[str]:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if staged else ["git", "ls-files"]
    return [f for f in subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout.split("\n") if f]


def _text(path: str, staged: bool) -> str | None:
    if staged:
        r = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True)
        data = r.stdout
    else:
        p = ROOT / path
        if not p.is_file():
            return None
        data = p.read_bytes()
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def main() -> int:
    staged = "--staged" in sys.argv
    terms = _terms()
    patterns = [(n, re.compile(p)) for n, p in GENERIC]
    if terms:
        patterns.append(("private term", re.compile(
            r"(?<![A-Za-z])(?:" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
            + r")(?![A-Za-z])", re.IGNORECASE)))
    hits = 0
    for f in _files(staged):
        if f in ALLOW_FILES or f.endswith((".lock",)):
            continue
        text = _text(f, staged)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, pat in patterns:
                m = pat.search(line)
                if m:
                    hits += 1
                    if hits <= 50:
                        print(f"{f}:{n}: {name}: {m.group(0)[:40]}")
    if hits:
        print(f"leak_scan: {hits} hit(s). Move private text into private/ or a private block.")
        return 1
    print(f"leak_scan: clean ({'staged' if staged else 'all tracked'} files; {len(terms)} private terms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
