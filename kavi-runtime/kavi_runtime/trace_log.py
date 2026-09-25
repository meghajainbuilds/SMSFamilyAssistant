"""Unified-trace logger — one JSONL row per inbound exchange.

Every iMessage / email that enters the runtime gets exactly one row capturing
the full going-forward context Megha needs for open-coded error analysis:

  - inbound: text + sender + source
  - context_at_decision: system-prompt snapshot (sha256 + full text), skill
    files loaded, corrections injected, durable facts active, top-N open tasks
  - llm_calls[]: every Anthropic call cascaded inside this exchange
    (call_type, model, input_text, output_text, token usage, latency)
  - tool_calls[]: every Graph / BlueBubbles / durable-fact tool call
    (tool name, args, result, duration)
  - outbound[]: decision_id + text + structural-check pass/fail per send
  - exchange_outcome: outbound_sent | skipped_silently | paused | errored

Pairs with the per-capability eval surfaces (eval-persona-outbound-judgments,
eval-inbox-judgments). Those continue writing alongside; this is the unified
view per exchange, joinable to those via decision_id.

Same contextvar pattern as `inbound_log.current_inbound_id`: handlers set
`current_trace` at the top of `imessage_received` / `email_arrived` via the
`Trace` context manager. Downstream code (claude_client, graph_client,
outbound_log) reads it via `Trace.current()` and records its slice. Every
record_* method is failure-safe — a trace_log exception MUST NEVER take down
the runtime path.

On `__exit__`, one row is appended to `evals/traces/exchanges.jsonl` via
`state.append_jsonl`. Subsequent reads stream the JSONL into the HTML viewer
for open coding (Phase A) or into LLM-as-judge (Week 2-3).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.state import append_jsonl

logger = logging.getLogger(__name__)


current_trace: ContextVar["Trace | None"] = ContextVar("current_trace", default=None)


_DEFAULT_EXCHANGES_PATH = "/Users/kavi/HomeOS/evals/traces/exchanges.jsonl"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trace_id() -> str:
    return f"x_{_utc_iso()}_{uuid.uuid4().hex[:8]}"


def _exchanges_path(config: dict) -> Path:
    """Resolve the exchanges JSONL path from config, falling back to default.

    Default applies when `config["paths"]["exchanges_jsonl"]` is absent so older
    configs keep working. The parent directory is created if missing.
    """
    try:
        raw = (config.get("paths") or {}).get("exchanges_jsonl") or _DEFAULT_EXCHANGES_PATH
    except Exception:
        raw = _DEFAULT_EXCHANGES_PATH
    p = Path(raw)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Filesystem hiccup — let append_jsonl surface the failure if it persists.
        pass
    return p


class Trace:
    """Context manager for one inbound exchange.

    Usage:
        with Trace(channel="imessage", participant=sender, config=config) as t:
            # downstream code reads via Trace.current() and records its slice
            ...

    Re-entrancy: nested `Trace` blocks are allowed but the inner one wins for
    `Trace.current()` resolution. On the inner `__exit__`, the contextvar is
    reset to the outer trace. The outer's row is unaffected — each trace owns
    its own JSONL row written at its own `__exit__`. In practice the runtime
    never nests traces (imessage_received and email_arrived are top-level
    entrypoints), so this is defensive only.

    Failure-safety: every method swallows exceptions and logs a warning. A
    trace_log failure MUST NEVER take down the runtime — same posture as
    `inbound_log.log_inbound`. The `__exit__` write failure is also swallowed.
    """

    def __init__(
        self,
        *,
        channel: str,
        participant: str | None = None,
        config: dict,
        capability: str = "kavi-persona",
        thread_id: str | None = None,
    ) -> None:
        self.trace_id = _trace_id()
        self.timestamp = _utc_iso()
        self.channel = channel
        self.participant = participant
        self.capability = capability
        self.thread_id = thread_id
        self.config = config
        self.inbound: dict[str, Any] = {}
        self.context_at_decision: dict[str, Any] = {
            "system_prompt_hash": None,
            "system_prompt_text": None,
            "skill_files_loaded": [],
            "corrections_injected": [],
            "durable_facts_active": [],
            "open_tasks_top_n": [],
        }
        self.llm_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.outbound: list[dict[str, Any]] = []
        self._exchange_outcome: str | None = None
        self._token: Any = None
        self._system_prompt_recorded: bool = False

    # ---- contextvar lifecycle --------------------------------------------

    def __enter__(self) -> "Trace":
        try:
            self._token = current_trace.set(self)
        except Exception as e:
            logger.warning("trace_log: __enter__ contextvar set failed err=%s", e)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            # Derive default outcome if not set explicitly.
            if self._exchange_outcome is None:
                if exc_type is not None:
                    self._exchange_outcome = "errored"
                elif self.outbound:
                    self._exchange_outcome = "outbound_sent"
                else:
                    self._exchange_outcome = "skipped_silently"
            record = {
                "trace_id": self.trace_id,
                "thread_id": self.thread_id,
                "timestamp": self.timestamp,
                "channel": self.channel,
                "capability": self.capability,
                "participant": self.participant,
                "inbound": self.inbound or None,
                "context_at_decision": self.context_at_decision,
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "outbound": self.outbound,
                "exchange_outcome": self._exchange_outcome,
                # Separate fact from the outcome (2026-09-23): an email can
                # become a task with no iMessage, so "did Kavi text anyone"
                # must not be inferred from exchange_outcome.
                "outbound_sent": bool(self.outbound),
            }
            append_jsonl(_exchanges_path(self.config), record)
        except Exception as e:
            logger.exception("trace_log: __exit__ write failed err=%s", e)
        finally:
            try:
                if self._token is not None:
                    current_trace.reset(self._token)
            except Exception:
                # Token may belong to a different context (rare; e.g., test
                # tear-down across event loops). Best-effort clear.
                try:
                    current_trace.set(None)
                except Exception:
                    pass

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def current() -> "Trace | None":
        """Return the active Trace or None. Cheap accessor for downstream code."""
        try:
            return current_trace.get()
        except Exception:
            return None

    def set_exchange_outcome(self, outcome: str) -> None:
        """Mark the outcome explicitly. iMessage exchanges use
        `outbound_sent | skipped_silently | paused | errored`; email exchanges
        (inbox-to-task, 2026-09-23) use `task_created | task_updated |
        dedup_hit | webhook_redup_hit | skipped | paused | errored`. If never called,
        `__exit__` derives the outcome from `outbound` (non-empty → sent;
        else skipped) and `exc_type` (non-None → errored).
        """
        try:
            self._exchange_outcome = outcome
        except Exception as e:
            logger.warning("trace_log: set_exchange_outcome failed err=%s", e)

    # ---- record_* methods (all failure-safe) -----------------------------

    def record_inbound(
        self,
        *,
        inbound_id: str | None = None,
        ts: str | None = None,
        text: str | None = None,
        sender: str | None = None,
        source: str | None = None,
        char_count: int | None = None,
    ) -> None:
        """Record the inbound message that opened this exchange. Mirrors the
        shape of `inbound_log.log_inbound`'s JSONL row so downstream tooling
        can read either surface with the same parsing."""
        try:
            self.inbound = {
                "inbound_id": inbound_id,
                "ts": ts,
                "text": text,
                "sender": sender,
                "source": source,
                "char_count": char_count if char_count is not None else (len(text) if text else None),
            }
        except Exception as e:
            logger.warning("trace_log: record_inbound failed err=%s", e)

    def record_llm_call(
        self,
        *,
        call_type: str,
        model: str,
        input_text: str | None = None,
        output_text: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        latency_ms: int | None = None,
        started_at: str | None = None,
    ) -> None:
        """Append one Anthropic call row to this trace. Mirrors the
        structured-log `anthropic.call_done` event shape so the two surfaces
        line up. `input_text`/`output_text` may be None when threaded callers
        haven't been updated to pass them (back-compat with legacy call sites).
        """
        try:
            self.llm_calls.append({
                "call_type": call_type,
                "model": model,
                "input_text": input_text,
                "output_text": output_text,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "latency_ms": latency_ms,
                "started_at": started_at,
            })
        except Exception as e:
            logger.warning("trace_log: record_llm_call failed err=%s", e)

    def record_tool_call(
        self,
        tool: str,
        args: Any,
        result: Any,
        *,
        duration_ms: int | None = None,
        started_at: str | None = None,
    ) -> None:
        """Append one tool-call row (Graph, BlueBubbles, durable-fact write).
        Args + result are kept loosely-typed; callers should pass JSON-safe
        dicts. Defensive: any non-serializable value is coerced to repr.
        """
        try:
            self.tool_calls.append({
                "tool": tool,
                "args": _safe_jsonable(args),
                "result": _safe_jsonable(result),
                "duration_ms": duration_ms,
                "started_at": started_at,
            })
        except Exception as e:
            logger.warning("trace_log: record_tool_call failed tool=%s err=%s", tool, e)

    def record_outbound(
        self,
        *,
        decision_id: str | None = None,
        kind: str | None = None,
        text: str | None = None,
        char_count: int | None = None,
        structural_checks: dict[str, Any] | None = None,
        structural_pass: bool | None = None,
        fallback_used: bool | None = None,
        verified: bool | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Append one outbound row. Mirrors `outbound_log.log_outbound` fields
        so the trace surface and the per-capability eval surface line up."""
        try:
            self.outbound.append({
                "decision_id": decision_id,
                "kind": kind,
                "text": text,
                "char_count": char_count if char_count is not None else (len(text) if text else None),
                "structural_checks": structural_checks,
                "structural_pass": structural_pass,
                "fallback_used": fallback_used,
                "verified": verified,
                "context": context or {},
            })
        except Exception as e:
            logger.warning("trace_log: record_outbound failed kind=%s err=%s", kind, e)

    def record_system_prompt(self, text: str | None) -> None:
        """Snapshot the system prompt once per trace. First call wins; subsequent
        calls within the same trace are no-ops (the system prompt doesn't change
        mid-exchange, and we only want one canonical snapshot per trace row).
        """
        try:
            if self._system_prompt_recorded:
                return
            if not text:
                return
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self.context_at_decision["system_prompt_hash"] = f"sha256:{digest}"
            self.context_at_decision["system_prompt_text"] = text
            self._system_prompt_recorded = True
        except Exception as e:
            logger.warning("trace_log: record_system_prompt failed err=%s", e)

    def record_context(
        self,
        *,
        corrections_injected: list[Any] | None = None,
        durable_facts_active: list[Any] | None = None,
        open_tasks_top_n: list[Any] | None = None,
        skill_files_loaded: list[str] | None = None,
    ) -> None:
        """Populate the at-decision context slice. Each field is optional;
        omitted fields preserve their current value (so callers can record
        these in any order across the exchange)."""
        try:
            if corrections_injected is not None:
                self.context_at_decision["corrections_injected"] = corrections_injected
            if durable_facts_active is not None:
                self.context_at_decision["durable_facts_active"] = durable_facts_active
            if open_tasks_top_n is not None:
                self.context_at_decision["open_tasks_top_n"] = open_tasks_top_n
            if skill_files_loaded is not None:
                self.context_at_decision["skill_files_loaded"] = skill_files_loaded
        except Exception as e:
            logger.warning("trace_log: record_context failed err=%s", e)


def current() -> Trace | None:
    """Convenience wrapper around `current_trace.get()`. Use this at insertion
    sites so callers don't have to import the contextvar directly."""
    try:
        return current_trace.get()
    except Exception:
        return None


def _safe_jsonable(value: Any) -> Any:
    """Best-effort coerce a value to JSON-safe shape. Dicts/lists/primitives
    pass through; anything else gets `repr()`'d. Defensive only — callers
    should pass clean dicts."""
    try:
        import json as _json
        _json.dumps(value, default=str)
        return value
    except Exception:
        try:
            return repr(value)
        except Exception:
            return "<unrepr>"
