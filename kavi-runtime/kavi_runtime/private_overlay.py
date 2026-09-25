"""Private overlay for public spec and skill files (public-repo step 2).

The capability docs (`capabilities/*.md`) and composer skills
(`kavi-runtime/skills/*.md`) are committed publicly. Family-specific spans
inside them (kids, schools, caregivers, vendors, real senders) are wrapped
in private blocks whose committed body is a fictional stand-in:

    <!-- private:itt-few-shot-3 -->fictional text<!-- /private -->

The real text lives in gitignored JSON files under `private/overlays/`
({block_id: original_text}). `resolve()` swaps every block for its real
text, so on Kavi the resolved file is byte-identical to the pre-split file
and the prompt Kavi sends does not change.

Where the overlay lives: found by walking up from the spec file to the
first ancestor holding `private/overlays/`. Locally that is
`HomeOS/private/`; on Kavi, `deploy.sh` ships it to
`$KAVI_RUNTIME_DIR/private/`.

Missing overlay: a file with private blocks and no overlay raises, the
same loud-failure contract as `persona_loader`. Kavi running on fictional
family examples would misroute real email, which is worse than crashing
the composer call. Public clones set `HOMEOS_PUBLIC_EXAMPLES=1` to run on
the fictional stand-ins instead.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

BLOCK_RE = re.compile(
    r"<!-- private:(?P<id>[A-Za-z0-9_.-]+) -->(?P<body>.*?)<!-- /private -->",
    re.DOTALL,
)
OVERLAY_SUBDIR = Path("private") / "overlays"
PUBLIC_EXAMPLES_ENV = "HOMEOS_PUBLIC_EXAMPLES"

_CACHE: dict[str, dict[str, str]] = {}
_LOCK = threading.Lock()


class PrivateOverlayError(RuntimeError):
    pass


def find_overlay_dir(start: str | Path) -> Path | None:
    p = Path(start).resolve()
    for d in [p, *p.parents]:
        cand = d / OVERLAY_SUBDIR
        if cand.is_dir():
            return cand
    return None


def load_overlay(overlay_dir: Path) -> dict[str, str]:
    key = str(overlay_dir)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        merged: dict[str, str] = {}
        for f in sorted(overlay_dir.glob("*.json")):
            for bid, text in json.loads(f.read_text()).items():
                if bid in merged:
                    raise PrivateOverlayError(f"duplicate private block id {bid!r} in {f}")
                merged[bid] = text
        _CACHE[key] = merged
        return merged


def has_blocks(text: str) -> bool:
    return BLOCK_RE.search(text) is not None


def resolve(text: str, source_path: str | Path, *, overlay: dict[str, str] | None = None) -> str:
    """Return `text` with every private block replaced by its real content.
    No-op for text without blocks."""
    if not has_blocks(text):
        return text
    if overlay is None:
        odir = find_overlay_dir(Path(source_path).parent)
        if odir is None:
            if os.environ.get(PUBLIC_EXAMPLES_ENV) == "1":
                return BLOCK_RE.sub(lambda m: m.group("body"), text)
            raise PrivateOverlayError(
                f"{source_path} has private blocks but no private/overlays/ directory "
                f"was found above it. On Kavi, deploy.sh must ship private/. For a "
                f"public clone, set {PUBLIC_EXAMPLES_ENV}=1."
            )
        overlay = load_overlay(odir)

    def _sub(m: re.Match[str]) -> str:
        bid = m.group("id")
        if bid not in overlay:
            raise PrivateOverlayError(f"private block {bid!r} in {source_path} has no overlay entry")
        return overlay[bid]

    return BLOCK_RE.sub(_sub, text)


def read_resolved(path: str | Path) -> str:
    """Read a spec or skill file with private blocks resolved."""
    p = Path(path)
    return resolve(p.read_text(), p)


def extract(original: str, public: str) -> dict[str, str]:
    """Derive the overlay entries for `public` from the pre-split `original`.

    `public` must equal `original` outside its private blocks. Each block's
    real text is the span of `original` between the surrounding fixed text.
    Raises if the round trip does not reproduce `original` exactly."""
    pieces: list[tuple[str, str]] = []  # (fixed_before, block_id)
    pos = 0
    for m in BLOCK_RE.finditer(public):
        pieces.append((public[pos:m.start()], m.group("id")))
        pos = m.end()
    tail = public[pos:]

    out: dict[str, str] = {}
    cur = 0
    for i, (fixed, bid) in enumerate(pieces):
        if not original.startswith(fixed, cur):
            raise PrivateOverlayError(f"text before block {bid!r} differs from the original")
        cur += len(fixed)
        nxt = pieces[i + 1][0] if i + 1 < len(pieces) else tail
        if nxt == "":
            if i + 1 < len(pieces):
                raise PrivateOverlayError(f"blocks {bid!r} and {pieces[i + 1][1]!r} are adjacent; merge them")
            end = len(original)
        else:
            end = original.find(nxt, cur) if i + 1 < len(pieces) else len(original) - len(tail)
            if end < cur:
                raise PrivateOverlayError(f"cannot locate text after block {bid!r} in the original")
        if bid in out:
            raise PrivateOverlayError(f"block id {bid!r} used twice")
        out[bid] = original[cur:end]
        cur = end
    if resolve(public, "<extract>", overlay=out) != original:
        raise PrivateOverlayError("round trip failed: resolved public text != original")
    return out
