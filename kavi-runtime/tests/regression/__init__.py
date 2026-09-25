"""Production-trace regression library.

Each fixture in `fixtures/` is a JSON snapshot of a real production
failure (or a real production happy-path). Each `test_<id>.py` file in
this directory loads the matching fixture and asserts on user-visible
outcome through the action-layer code path with mocked Anthropic +
mocked Graph.

Discipline rule (engineering-discipline.md §9): every production
failure becomes a permanent fixture before the fix ships. This library
is where they live.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest -m regression
"""
