#!/usr/bin/env python3
"""Maintain the private overlay for public spec and skill files.

    extract <file>...          derive overlay entries from the file at --base
                               (default HEAD) and write private/overlays/<slug>.json
    check [--against SHA]      every block resolves; with --against, every
                               resolved file equals that commit's version
    scan                       list denylisted terms still visible in public text
    show <file>                print the file with private blocks resolved

Run from anywhere; paths are relative to the HomeOS repo root.
See kavi_runtime/private_overlay.py for the block format.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "kavi-runtime"))

from kavi_runtime import private_overlay as po  # noqa: E402

OVERLAY_DIR = ROOT / po.OVERLAY_SUBDIR
DENYLIST = ROOT / "private" / "denylist.txt"
SPEC_GLOBS = ["capabilities/*.md", "kavi-runtime/skills/*.md"]


def _git_show(sha: str, rel: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{sha}:{rel}"],
        check=True, capture_output=True, text=True,
    ).stdout


def _slug(rel: str) -> str:
    return rel.replace("/", "__").removesuffix(".md")


def _spec_files() -> list[Path]:
    return sorted(p for g in SPEC_GLOBS for p in ROOT.glob(g))


def cmd_extract(files: list[str], base: str) -> int:
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    for f in files:
        rel = str(Path(f).resolve().relative_to(ROOT))
        public = (ROOT / rel).read_text()
        # The base may already contain private blocks (a file split earlier);
        # resolve them with the current overlay first, or the stand-in text
        # would be recorded as the "real" text (2026-09-25 bug).
        base_text = _git_show(base, rel)
        if po.has_blocks(base_text):
            base_text = po.resolve(base_text, ROOT / rel, overlay=po.load_overlay(OVERLAY_DIR))
        entries = po.extract(base_text, public)
        out = OVERLAY_DIR / f"{_slug(rel)}.json"
        if entries:
            out.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n")
            print(f"{rel}: {len(entries)} blocks -> {out.relative_to(ROOT)}")
        elif out.exists():
            out.unlink()
    po._CACHE.clear()
    return 0


def cmd_check(against: str | None) -> int:
    bad = 0
    overlay = po.load_overlay(OVERLAY_DIR) if OVERLAY_DIR.is_dir() else {}
    for p in _spec_files():
        rel = str(p.relative_to(ROOT))
        try:
            resolved = po.resolve(p.read_text(), p, overlay=overlay)
        except po.PrivateOverlayError as e:
            print(f"FAIL {rel}: {e}")
            bad += 1
            continue
        if against:
            try:
                orig = _git_show(against, rel)
            except subprocess.CalledProcessError:
                continue  # file is new since `against`
            if resolved != orig:
                print(f"FAIL {rel}: resolved text differs from {against}")
                bad += 1
    print("check:", "OK" if not bad else f"{bad} failing")
    return 1 if bad else 0


def _terms() -> list[str]:
    if not DENYLIST.exists():
        sys.exit(f"no denylist at {DENYLIST}")
    return [t.strip() for t in DENYLIST.read_text().splitlines() if t.strip() and not t.startswith("#")]


def denylist_pattern(terms: list[str]) -> re.Pattern[str]:
    # Whole words, any case: "Ann" must not match "planning", but "maple"
    # and "MAPLE" count. Only letters bound a term, so "+442071234567"
    # still matches "2071234567".
    return re.compile(
        r"(?<![A-Za-z])(?:" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")(?![A-Za-z])",
        re.IGNORECASE,
    )


def cmd_scan() -> int:
    terms = _terms()
    pat = denylist_pattern(terms)
    hits = 0
    for p in _spec_files():
        public = po.BLOCK_RE.sub(lambda m: m.group("body"), p.read_text())
        for n, line in enumerate(public.splitlines(), 1):
            for m in pat.finditer(line):
                print(f"{p.relative_to(ROOT)}:{n}: {m.group(0)}")
                hits += 1
    print(f"scan: {hits} hits")
    return 1 if hits else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract"); e.add_argument("files", nargs="+"); e.add_argument("--base", default="HEAD")
    c = sub.add_parser("check"); c.add_argument("--against")
    sub.add_parser("scan")
    s = sub.add_parser("show"); s.add_argument("file")
    a = ap.parse_args()
    if a.cmd == "extract":
        return cmd_extract(a.files, a.base)
    if a.cmd == "check":
        return cmd_check(a.against)
    if a.cmd == "scan":
        return cmd_scan()
    print(po.read_resolved(a.file), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
