"""Private overlay (public-repo step 2): public specs carry fictional
stand-ins inside private blocks; the gitignored overlay restores the real
text byte for byte."""

import json
from pathlib import Path

import pytest

from kavi_runtime import private_overlay as po

ROOT = Path(__file__).resolve().parents[2]

ORIGINAL = "Rule A.\nKid Real goes to Real School on Fridays.\nRule B.\n"
PUBLIC = (
    "Rule A.\n<!-- private:ex-1 -->Kid Theo goes to Maple School on Fridays.\n"
    "<!-- /private -->Rule B.\n"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    po._CACHE.clear()
    yield
    po._CACHE.clear()


def _tree(tmp_path, overlay: dict | None):
    spec = tmp_path / "capabilities" / "x.md"
    spec.parent.mkdir()
    spec.write_text(PUBLIC)
    if overlay is not None:
        od = tmp_path / "private" / "overlays"
        od.mkdir(parents=True)
        (od / "capabilities__x.json").write_text(json.dumps(overlay))
    return spec


def test_extract_round_trips():
    assert po.extract(ORIGINAL, PUBLIC) == {"ex-1": "Kid Real goes to Real School on Fridays.\n"}


def test_extract_rejects_edits_outside_blocks():
    with pytest.raises(po.PrivateOverlayError):
        po.extract(ORIGINAL, PUBLIC.replace("Rule B.", "Rule C."))


def test_resolve_restores_original(tmp_path):
    spec = _tree(tmp_path, po.extract(ORIGINAL, PUBLIC))
    assert po.read_resolved(spec) == ORIGINAL


def test_missing_overlay_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv(po.PUBLIC_EXAMPLES_ENV, raising=False)
    spec = _tree(tmp_path, None)
    with pytest.raises(po.PrivateOverlayError):
        po.read_resolved(spec)


def test_public_clone_uses_fictional_stand_ins(tmp_path, monkeypatch):
    monkeypatch.setenv(po.PUBLIC_EXAMPLES_ENV, "1")
    spec = _tree(tmp_path, None)
    assert "Theo" in po.read_resolved(spec)
    assert "<!--" not in po.read_resolved(spec)


def test_missing_block_id_fails(tmp_path):
    spec = _tree(tmp_path, {"other": "x"})
    with pytest.raises(po.PrivateOverlayError):
        po.read_resolved(spec)


def test_text_without_blocks_is_untouched(tmp_path):
    p = tmp_path / "plain.md"
    p.write_text(ORIGINAL)
    assert po.read_resolved(p) == ORIGINAL


def test_every_repo_block_resolves():
    """Every private block in the committed specs and skills has an overlay
    entry. Skipped on a public clone, where the overlay does not exist."""
    od = ROOT / po.OVERLAY_SUBDIR
    if not od.is_dir():
        pytest.skip("no private overlay (public clone)")
    overlay = po.load_overlay(od)
    for g in ("capabilities/*.md", "kavi-runtime/skills/*.md"):
        for p in ROOT.glob(g):
            po.resolve(p.read_text(), p, overlay=overlay)
