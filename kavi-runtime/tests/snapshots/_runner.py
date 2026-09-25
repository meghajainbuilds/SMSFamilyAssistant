"""Live-LLM snapshot runner helpers.

Each snapshot test follows this shape:
  1. Call a real ClaudeClient method against fixture input.
  2. Assert the return value is non-None and matches the documented schema.
  3. Assert the captured `output_tokens` is well below the call's
     hardcoded `max_tokens` (50-token margin) — drift past that margin
     is the 2026-05-08 max_tokens=400 clip class of bug.

The runner captures usage by monkey-patching `claude_client.log_event`
(which the wrappers call after every API response). It records the
last call_done event for each call_type seen during the test.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest


class UsageRecorder:
    """Captures the last call_done usage row per call_type. Use as a
    monkeypatch substitute for `kavi_runtime.claude_client.log_event`.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        # Wrappers call: log_event("anthropic", "call_done", call_type=..., model=...,
        #                          input_tokens=..., output_tokens=..., latency_ms=...).
        if len(args) >= 2 and args[0] == "anthropic":
            self.events.append({"event": args[1], **kwargs})

    def last_for(self, call_type: str, event: str = "call_done") -> dict[str, Any] | None:
        for e in reversed(self.events):
            if e.get("event") == event and e.get("call_type") == call_type:
                return e
        return None


def assert_output_tokens_well_below_cap(
    recorder: UsageRecorder,
    *,
    call_type: str,
    cap: int,
    margin: int = 50,
) -> None:
    """Assert that the last `call_done` event for `call_type` had
    output_tokens at least `margin` below `cap`. Lower margin and the
    response is one prompt edit away from clipping mid-JSON."""
    e = recorder.last_for(call_type)
    assert e is not None, f"no call_done event for {call_type}"
    out = e.get("output_tokens")
    assert isinstance(out, int), (
        f"output_tokens missing or non-int on call_done for {call_type}: {e!r}"
    )
    assert out <= cap - margin, (
        f"{call_type} output_tokens={out} too close to max_tokens={cap} "
        f"(margin={margin}); a prompt edit could clip the response. "
        f"Either raise the cap or shrink the prompt."
    )


def install_usage_recorder(monkeypatch: pytest.MonkeyPatch) -> UsageRecorder:
    """Install the recorder and return it. Call once per test."""
    from kavi_runtime import claude_client as cc_mod

    recorder = UsageRecorder()
    # Wrap rather than replace: keep the original behavior (the
    # structured log writer) AND record. log_event in claude_client
    # already swallows its own exceptions so wrapping is safe.
    original = cc_mod.log_event

    def _wrapper(*args: Any, **kwargs: Any) -> None:
        recorder(*args, **kwargs)
        try:
            original(*args, **kwargs)
        except Exception:
            pass

    monkeypatch.setattr(cc_mod, "log_event", _wrapper)
    return recorder
