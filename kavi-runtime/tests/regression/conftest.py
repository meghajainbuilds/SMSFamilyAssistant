"""Regression fixture loader.

Reads `*.json` files from `fixtures/` and exposes them as a `regression_fixture`
fixture (parameterised by id). Each fixture file shape:

    {
      "id": "<date>_<short_desc>",
      "description": "...",
      "inbound": {"text": "...", "sender": "...", "ts": "..."},
      "graph_state": {"open_tasks": [...]},
      "expected_outcome": {...},
      "regression_for_commit": "<sha>"
    }

A `mock_llm_responses` block can also live in the fixture; tests that
need it pull it out themselves so each test can encode its own assertion
shape against a per-skill response. The loader's job is plumbing only.

The `regression` pytest marker (registered in pyproject.toml /
conftest.py via `pytest_collection_modifyitems`) tags every test file in
this directory so `pytest -m regression` runs the library as a unit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(fixture_id: str) -> dict[str, Any]:
    """Load a single fixture by id. Raises FileNotFoundError if absent."""
    path = FIXTURES_DIR / f"{fixture_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"regression fixture {fixture_id!r} not found at {path}; "
            f"add it to tests/regression/fixtures/"
        )
    return json.loads(path.read_text())


def all_fixture_ids() -> list[str]:
    """Every fixture id available in fixtures/. Used by parametrised tests."""
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))


@pytest.fixture
def regression_fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Per-test fixture loader. Tests pass the fixture id via a marker:

        @pytest.mark.regression_fixture_id("2026-05-08_apostrophe_tokenizer_zero_matches")
        def test_..., regression_fixture):
            assert regression_fixture["id"] == ...
    """
    marker = request.node.get_closest_marker("regression_fixture_id")
    if marker is None:
        raise RuntimeError(
            "regression_fixture requires @pytest.mark.regression_fixture_id(<id>)"
        )
    return load_fixture(marker.args[0])


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-tag every test in this dir with the `regression` marker so
    `pytest -m regression` picks them up without each file repeating it."""
    regression_dir = str(Path(__file__).parent.resolve())
    for item in items:
        if regression_dir in str(Path(item.fspath).resolve()):
            item.add_marker(pytest.mark.regression)


def pytest_configure(config: pytest.Config) -> None:
    """Register the `regression` and `regression_fixture_id` markers so
    pytest doesn't emit warnings on `-m regression` runs."""
    config.addinivalue_line(
        "markers",
        "regression: production-trace regression library "
        "(see kavi-runtime/tests/regression/__init__.py)",
    )
    config.addinivalue_line(
        "markers",
        "regression_fixture_id(id): id of the JSON fixture this test loads",
    )
