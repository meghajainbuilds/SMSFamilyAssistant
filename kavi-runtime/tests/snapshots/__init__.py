"""Live-LLM snapshot tests.

Calls the real Anthropic API against each action-layer skill and
asserts response shape (parses cleanly, fits the schema documented in
the skill, output_tokens stays under the configured cap).

These tests are skipped by default. Run with:

    cd kavi-runtime && .venv/bin/python -m pytest -m live_llm

Cost per full run: ~$0.05 - $0.20 (small inputs, small outputs across
8 skills). Designed to run nightly via launchd on Kavi.

What this catches that mocked tests don't:
  - Prompt drift: a skill edit makes the LLM start wrapping output in
    ```json fences, breaks downstream parsers.
  - max_tokens drift: a skill grows long enough that real outputs hit
    the cap and clip mid-JSON (the 2026-05-08 max_tokens=400 bug).
  - Schema regression: a skill's output stops matching what the
    runtime parser expects.

This is the layer between mocked unit tests (cheap, fast, can lie
about real LLM behavior) and Megha-discovers-it-in-prod (slow, costly).
"""
