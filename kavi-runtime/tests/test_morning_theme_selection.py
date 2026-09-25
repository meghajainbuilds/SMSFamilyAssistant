"""Tests for the morning top-of-mind theme selection (2026-06-10).

Contract locked here (capabilities/kavi_persona/selection.py:
_select_morning_theme):

  * Fewer than THEME_MIN_CLUSTER open tasks → None, no LLM call.
  * Clustering input capped at THEME_MAX_INPUT_TITLES titles, each
    truncated to THEME_TITLE_TRUNCATE_CHARS chars.
  * Result (theme OR the model's no-theme) caches per recipient per
    Pacific day in morning_theme.json — one LLM call per recipient per
    morning.
  * API/parse failure → None and NOT cached (tomorrow retries; today's
    later fires retry too).
  * supporting_task_titles validated against the actual input; validated
    support below THEME_MIN_CLUSTER → no theme (conservative).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_morning_theme_selection.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capabilities.kavi_persona import selection
from kavi_runtime.runtime import clients as _clients_mod


def _cfg(tmp_path: Path) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return {
        "paths": {"imessage_state": str(state_dir / "imessage-state.json")},
    }


_CAMP_TITLES = [
    "MJ Register for Cascade summer camp",
    "MJ Pay camp deposit",
    "MJ Order swim gear for camp",
    "MJ Pay Boonli invoice",
]


def _tasks(titles: list[str]) -> list[dict[str, Any]]:
    return [{"id": f"t-{i}", "title": t} for i, t in enumerate(titles)]


class _StubClaude:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {
            "theme": {
                "label": "Summer camp planning",
                "supporting_task_titles": _CAMP_TITLES[:3],
            },
            "_usage": {"input_tokens": 1500},
        }

    def cluster_morning_theme(self, titles: list[str], today: str) -> dict[str, Any]:
        self.calls.append({"titles": titles, "today": today})
        return dict(self.result)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> _StubClaude:
    stub = _StubClaude()
    monkeypatch.setattr(
        _clients_mod, "_get_clients", lambda c: (None, stub, None),
    )
    return stub


# ---- min-cluster gate ---------------------------------------------------------


def test_below_min_cluster_returns_none_without_llm(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    tasks = _tasks(_CAMP_TITLES[: selection.THEME_MIN_CLUSTER - 1])
    out = selection._select_morning_theme(
        _cfg(tmp_path), tasks, recipient="megha",
    )
    assert out is None
    assert claude.calls == []


def test_none_and_empty_inputs(tmp_path: Path, claude: _StubClaude) -> None:
    cfg = _cfg(tmp_path)
    assert selection._select_morning_theme(cfg, None, recipient="megha") is None
    assert selection._select_morning_theme(cfg, [], recipient="megha") is None
    assert claude.calls == []


# ---- input caps ------------------------------------------------------------------


def test_input_capped_at_max_titles_each_truncated(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    long_titles = [f"MJ Task {i} " + "x" * 200 for i in range(55)]
    selection._select_morning_theme(
        _cfg(tmp_path), _tasks(long_titles), recipient="megha",
    )
    assert len(claude.calls) == 1
    sent = claude.calls[0]["titles"]
    assert len(sent) == selection.THEME_MAX_INPUT_TITLES
    assert all(len(t) <= selection.THEME_TITLE_TRUNCATE_CHARS for t in sent)


# ---- per-day per-recipient cache ----------------------------------------------


def test_clustering_runs_once_per_morning_per_recipient(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    cfg = _cfg(tmp_path)
    tasks = _tasks(_CAMP_TITLES)

    first = selection._select_morning_theme(cfg, tasks, recipient="megha")
    second = selection._select_morning_theme(cfg, tasks, recipient="megha")

    assert len(claude.calls) == 1, "same recipient same day must hit cache"
    assert first == second
    assert first is not None
    assert first["label"] == "Summer camp planning"
    assert first["task_count"] == 3

    # Other recipient: own clustering call, own cache slot.
    selection._select_morning_theme(cfg, tasks, recipient="max")
    assert len(claude.calls) == 2

    state = json.loads(selection._morning_theme_state_path(cfg).read_text())
    assert set(state) == {"megha", "max"}


def test_no_theme_verdict_is_cached_for_the_day(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    cfg = _cfg(tmp_path)
    claude.result = {"theme": None, "_usage": {}}
    tasks = _tasks(_CAMP_TITLES)

    assert selection._select_morning_theme(cfg, tasks, recipient="megha") is None
    assert selection._select_morning_theme(cfg, tasks, recipient="megha") is None
    assert len(claude.calls) == 1, "the model's no-theme is a verdict; cache it"


def test_llm_failure_returns_none_and_is_not_cached(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    cfg = _cfg(tmp_path)
    claude.result = {"theme": None, "_usage": None, "_error": "api_error: boom"}
    tasks = _tasks(_CAMP_TITLES)

    assert selection._select_morning_theme(cfg, tasks, recipient="megha") is None
    assert selection._select_morning_theme(cfg, tasks, recipient="megha") is None
    assert len(claude.calls) == 2, "transient failure must not poison the day"
    assert not selection._morning_theme_state_path(cfg).exists()


def test_clients_exception_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(config: dict) -> Any:
        raise RuntimeError("no clients")

    monkeypatch.setattr(_clients_mod, "_get_clients", _boom)
    out = selection._select_morning_theme(
        _cfg(tmp_path), _tasks(_CAMP_TITLES), recipient="megha",
    )
    assert out is None


# ---- supporting-title validation -----------------------------------------------


def test_supporting_titles_must_come_from_the_input(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    """A theme propped up by invented titles is treated as no theme."""
    cfg = _cfg(tmp_path)
    claude.result = {
        "theme": {
            "label": "Summer camp planning",
            "supporting_task_titles": [
                "MJ Register for Cascade summer camp",  # real
                "MJ Invented camp task A",               # not in input
                "MJ Invented camp task B",               # not in input
            ],
        },
        "_usage": {},
    }
    out = selection._select_morning_theme(
        cfg, _tasks(_CAMP_TITLES), recipient="megha",
    )
    assert out is None


def test_validated_support_below_min_cluster_drops_theme(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    cfg = _cfg(tmp_path)
    claude.result = {
        "theme": {
            "label": "Summer camp planning",
            "supporting_task_titles": _CAMP_TITLES[:2],  # only 2 < min 3
        },
        "_usage": {},
    }
    out = selection._select_morning_theme(
        cfg, _tasks(_CAMP_TITLES), recipient="megha",
    )
    assert out is None
    # The weak-theme verdict is still a verdict: cached as no-theme.
    assert selection._select_morning_theme(
        cfg, _tasks(_CAMP_TITLES), recipient="megha",
    ) is None
    assert len(claude.calls) == 1


def test_theme_shape_is_label_plus_validated_task_count(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    out = selection._select_morning_theme(
        _cfg(tmp_path), _tasks(_CAMP_TITLES), recipient="megha",
    )
    assert out is not None
    assert out["label"] == "Summer camp planning"
    assert out["task_count"] == 3
    # Member titles ride along so the composer can name the concrete tasks
    # (2026-06-22), not just a count.
    assert set(out["task_titles"]) == set(_CAMP_TITLES[:3])


def test_theme_surfaces_member_task_titles_oldest_first(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    """When a theme fires, task_titles is populated with the validated
    member titles, capped and oldest-first.

    `_CAMP_TITLES` is fetch order (newest first), so oldest-first reverses
    the input order of the surfaced subset.
    """
    out = selection._select_morning_theme(
        _cfg(tmp_path), _tasks(_CAMP_TITLES), recipient="megha",
    )
    assert out is not None
    assert out["task_titles"]  # non-empty when a theme fires
    assert len(out["task_titles"]) <= selection.THEME_SURFACE_MAX_TITLES
    # Stub returns _CAMP_TITLES[:3] (indices 0,1,2 in newest-first input);
    # oldest-first = reversed.
    assert out["task_titles"] == list(reversed(_CAMP_TITLES[:3]))


def test_task_titles_capped_at_surface_max(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    """A cluster larger than the surface cap is trimmed to the cap; the
    count still reflects the full validated support."""
    big = [f"MJ Camp task {i}" for i in range(8)]
    claude.result = {
        "theme": {
            "label": "Summer camp planning",
            "supporting_task_titles": big,
        },
        "_usage": {},
    }
    out = selection._select_morning_theme(
        _cfg(tmp_path), _tasks(big), recipient="megha",
    )
    assert out is not None
    assert out["task_count"] == 8
    assert len(out["task_titles"]) == selection.THEME_SURFACE_MAX_TITLES


def test_state_dir_has_no_stray_tmp_files(
    tmp_path: Path, claude: _StubClaude,
) -> None:
    cfg = _cfg(tmp_path)
    selection._select_morning_theme(cfg, _tasks(_CAMP_TITLES), recipient="megha")
    state_path = selection._morning_theme_state_path(cfg)
    assert state_path.exists()
    assert list(state_path.parent.glob("*.tmp")) == []
