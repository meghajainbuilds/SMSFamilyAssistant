"""Anthropic SDK wrapper. Email-to-tasks judgment + correction classification + summary
composition. Uses prompt caching on the system prompt + household.md + skill prose.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import time

from anthropic import Anthropic

from kavi_runtime import secrets as kavi_secrets
from kavi_runtime.persona_loader import load_persona_text
from kavi_runtime.security_baseline import load_security_baseline_text
from kavi_runtime.structural_checks import LENGTH_CAP_HARD, LENGTH_CAP_TARGET
from kavi_runtime.structured_log import log_event

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list[dict[str, Any]], user_msg: str) -> int:
    """Cheap char/4 estimate over the prompt for the call_start log row.
    The accurate count comes back on resp.usage; this is only meant as a
    pre-call sanity number."""
    try:
        system_chars = sum(len(b.get("text", "")) for b in system if isinstance(b, dict))
    except Exception:
        system_chars = 0
    return (system_chars + len(user_msg or "")) // 4


def _safe_clarify_fallback_question(
    *,
    free_text: str,
    action_type: str,
    target_text: str | None,
    open_tasks: list[dict[str, Any]] | None,
) -> str:
    """Deterministic fallback question when the clarifying composer's LLM
    output trips the forbidden-state-claim check (Fix 2, 2026-05-08).

    Builds an honest question keyed on the inbound — never claims state.
    Compact: stays under the 240-char cap. Voice rules: prose, first-person,
    no signoff. Used only when the LLM-composed output had to be rewritten.

    AUDIT 2026-05-29: rules honored at this date (prose, first-person, no
    signoff, no status board, no action claim, ≤240 chars). Re-audit when
    persona spec, structural_checks gates, or this composer's skill
    changes. Cold-fallback audit (Phase 1 of EM-critique refactor)
    confirmed this template is honest and safe; kept in place.
    """
    target = (target_text or "").strip()
    n_open = len(open_tasks or [])
    if action_type == "mark_done":
        if n_open == 0:
            return "I haven't run anything yet — can you point me at the specific task?"
        if target:
            return (
                f"I haven't run anything yet — name the exact task to mark done? "
                f"You said '{target[:40]}'."
            )[:240]
        return (
            "I haven't run anything yet — name the exact task(s) to mark done?"
        )
    if action_type in {"update", "cancel"}:
        return (
            "I haven't run anything yet — can you tell me which task and what "
            "to change?"
        )
    return "I haven't run anything yet — can you give me a bit more detail?"


def _resolve_anthropic_api_key() -> str:
    """Resolve the Anthropic API key from Keychain first, env var second.

    Order (2026-05-06 secrets-module wiring):
      1. macOS Keychain via `secrets.read_secret("anthropic_api_key")`.
      2. `ANTHROPIC_API_KEY` environment variable.

    A KeyError is raised on absence so the runtime fails fast at startup
    instead of silently constructing an Anthropic client without auth.
    """
    try:
        kc_value = kavi_secrets.read_secret("anthropic_api_key")
    except Exception as e:
        logger.warning("claude_client: secrets.read_secret failed for anthropic_api_key: %s", e)
        kc_value = None
    if kc_value:
        return kc_value
    env_value = os.environ.get("ANTHROPIC_API_KEY")
    if env_value:
        return env_value
    raise KeyError(
        "anthropic_api_key not in macOS Keychain and ANTHROPIC_API_KEY env var unset; "
        "run scripts/migrate_secrets_to_keychain.py or export the env var"
    )


class ClaudeClient:
    def __init__(self, config: dict):
        self._anthropic = Anthropic(api_key=_resolve_anthropic_api_key())
        self._config = config  # for the post-call spend hook (spend_state.py)
        self._model = config["claude"]["model"]
        self._max_tokens = config["claude"]["max_tokens"]
        self._caching = config["claude"]["enable_prompt_caching"]
        self._skills_dir = Path(config["paths"]["skills_dir"])
        # Model routing (added 2026-05-06): per-call-type override map. When
        # absent or missing a key, calls fall through to claude.model. Lets
        # us downshift cheap classifier calls to Haiku without touching the
        # Sonnet-quality composer paths.
        self._model_routing = config.get("model_routing", {}) or {}
        # Per-call-type max_tokens routing (added 2026-05-06 per audits/
        # token_optimization_2026-05-06.md REC-4). Same lookup shape as model
        # routing; missing keys fall through to claude.max_tokens. Used today
        # to cap compose_email_to_tasks_judgment at 512 (P95 output 284) for
        # blast-radius reduction on jailbreak attempts.
        self._max_tokens_routing = config.get("max_tokens_routing", {}) or {}
        self._household_md_path = Path(config["paths"]["household_md"])
        # Optional path: capability behavior doc (Hard rules / Examples / Q&A learned).
        # Falls back to None if not configured for backwards-compat with older configs.
        _itt = config["paths"].get("inbox_to_task_md")
        self._inbox_to_task_md_path = Path(_itt) if _itt else None
        # Persona spec path (added 2026-05-27 as part of the spec-IS-the-
        # runtime collapse). Optional for back-compat: callers that omit the
        # path get composer calls without a loader-sourced persona block —
        # but every production config must set this, otherwise composer
        # output reverts to whatever voice the skill file alone provides.
        # See kavi_runtime/persona_loader.py for the extraction contract.
        _persona = config["paths"].get("kavi_persona_md")
        self._persona_md_path = Path(_persona) if _persona else None
        # Security baseline spec path (added 2026-05-28 as the second leg of
        # the spec-IS-the-runtime collapse). Same shape as the persona path:
        # optional for back-compat with older test configs; required in
        # production. The loader at kavi_runtime/security_baseline.py
        # extracts the System prompt section of capabilities/security-
        # baseline.md and returns it for inclusion in every composer call,
        # replacing the prior PERSONA_REFUSAL_LAYER constant.
        _security = config["paths"].get("security_baseline_md")
        self._security_baseline_md_path = Path(_security) if _security else None
        self._per_call_input_token_cap = config.get("guardrails", {}).get("per_call_input_token_cap", 50000)



    # ---- Thin wrappers for runtime.system_prompt helpers ----
    # Bodies live in kavi_runtime/runtime/system_prompt.py. Keep these
    # as instance methods so existing callers (composer functions in
    # capability files using `client._build_system_prompt(...)`) work
    # without name churn.

    def _skill(self, name):
        from kavi_runtime.runtime.system_prompt import _skill as _impl
        return _impl(self, name)

    def _household(self):
        from kavi_runtime.runtime.system_prompt import _household as _impl
        return _impl(self)

    def _inbox_to_task(self):
        from kavi_runtime.runtime.system_prompt import _inbox_to_task as _impl
        return _impl(self)

    def _persona(self):
        from kavi_runtime.runtime.system_prompt import _persona as _impl
        return _impl(self)

    def _security_baseline(self):
        from kavi_runtime.runtime.system_prompt import _security_baseline as _impl
        return _impl(self)

    def _build_system_prompt(self, skill_name, *, with_refusal_layer=True, cache_ttl="5m",
                             with_persona=None, with_capability_doc=False):
        from kavi_runtime.runtime.system_prompt import _build_system_prompt as _impl
        return _impl(self, skill_name, with_refusal_layer=with_refusal_layer, cache_ttl=cache_ttl,
                     with_persona=with_persona, with_capability_doc=with_capability_doc)

    def _persona_system_block(self, system_text):
        from kavi_runtime.runtime.system_prompt import _persona_system_block as _impl
        return _impl(self, system_text)

    def _voice_capped_call(self, user_msg, system, debug_label, max_tokens=300, char_cap=180, call_type=None):
        from kavi_runtime.runtime.system_prompt import _voice_capped_call as _impl
        return _impl(self, user_msg, system, debug_label, max_tokens=max_tokens, char_cap=char_cap, call_type=call_type)

    # ---- __getattr__ dispatch for capability composers/classifiers ----
    #
    # Phase 4 (2026-06-02) physical move: every compose_*/classify_* method
    # body lives in capabilities/<name>/. ClaudeClient resolves attribute
    # access dynamically so legacy callers (`claude.compose_X(...)`) keep
    # working without storing the body here.
    _CAPABILITY_DISPATCH = {
        "check_semantic_duplicate": ("capabilities.inbox_to_task.dedup", "check_semantic_duplicate"),
        "classify_action_intent": ("capabilities.kavi_persona.actions.intent_classifier", "classify_action_intent"),
        "classify_coordination_course_correction": ("capabilities.coordination.composers.course_correction", "classify_coordination_course_correction"),
        "classify_coordination_intent": ("capabilities.coordination.composers.intent_classifier", "classify_coordination_intent"),
        "classify_correction": ("capabilities.kavi_persona.composers.correction_classifier", "classify_correction"),
        "classify_pause_intent": ("kavi_runtime.runtime.pause_intent", "classify_pause_intent"),
        # classify_qa_reply DELETED 2026-06-10 (intent-first dispatch rebuild):
        # fully replaced by parse_reply_intents; skill file removed.
        "classify_self_check_reply": ("kavi_runtime.runtime.weekly_self_check_compose", "classify_self_check_reply"),
        "cluster_morning_theme": ("capabilities.kavi_persona.composers.theme_clusterer", "cluster_morning_theme"),
        "compose_action_clarifying_reply": ("capabilities.kavi_persona.composers.action_clarifying", "compose_action_clarifying_reply"),
        "compose_batch_action_reply": ("capabilities.kavi_persona.composers.batch_action", "compose_batch_action_reply"),
        "compose_conversational_reply": ("capabilities.kavi_persona.composers.conversational", "compose_conversational_reply"),
        "compose_coordination_ack": ("capabilities.coordination.composers.ack", "compose_coordination_ack"),
        "compose_coordination_addressee_message": ("capabilities.coordination.composers.addressee_message", "compose_coordination_addressee_message"),
        "compose_coordination_outcome": ("capabilities.coordination.composers.outcome", "compose_coordination_outcome"),
        "compose_kavi_reply": ("capabilities.kavi_persona.composers.reply", "compose_kavi_reply"),
        "compose_periodic_summary": ("capabilities.kavi_persona.composers.periodic_summary", "compose_periodic_summary"),
        "compose_post_action_reply": ("capabilities.kavi_persona.composers.post_action", "compose_post_action_reply"),
        "compose_qa_question": ("capabilities.kavi_persona.qa_loop.qa_question", "compose_qa_question"),
        "compose_weekly_self_check": ("kavi_runtime.runtime.weekly_self_check_compose", "compose_weekly_self_check"),
        "judge_close_suggestion": ("capabilities.kavi_persona.composers.close_suggestion_judge", "judge_close_suggestion"),
        "match_target_to_open_task": ("capabilities.kavi_persona.actions.target_matcher", "match_target_to_open_task"),
        "parse_coordination_reply": ("capabilities.coordination.composers.reply_parser", "parse_coordination_reply"),
        "parse_reply_intents": ("capabilities.kavi_persona.reply_intent_parser", "parse_reply_intents"),
        "resolve_pending_action_clarification": ("capabilities.kavi_persona.actions.resolve_pending", "resolve_pending_action_clarification"),
        "run_email_to_tasks": ("capabilities.inbox_to_task.compose", "run_email_to_tasks"),
    }

    def __getattr__(self, name):
        # Only invoked when normal attribute lookup fails (so methods/attrs
        # defined on self / class still resolve normally).
        if name in type(self)._CAPABILITY_DISPATCH:
            module_path, func_name = type(self)._CAPABILITY_DISPATCH[name]
            import importlib
            mod = importlib.import_module(module_path)
            impl = getattr(mod, func_name)
            def _bound(*args, **kwargs):
                return impl(self, *args, **kwargs)
            return _bound
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r} "
            f"(not in capability dispatch table either)"
        )

    def _log_call_start(self, call_type: str, model: str, est_input_tokens: int) -> float:
        """Emit the structured `anthropic.call_start` event and return a
        monotonic-time anchor for latency measurement on the corresponding
        call_done/call_failed row. No-throw — structured-log failures must
        never block the API call."""
        try:
            log_event(
                "anthropic", "call_start",
                call_type=call_type, model=model,
                est_input_tokens=est_input_tokens,
            )
        except Exception:
            logger.debug("structured_log call_start emit failed (continuing)")
        return time.monotonic()

    def _log_call_done(self, call_type: str, model: str, started_at: float,
                        usage: dict[str, Any] | None,
                        *,
                        input_text: str | None = None,
                        output_text: str | None = None,
                        resp: Any = None,
                        user_msg: str | None = None) -> None:
        """Emit `anthropic.call_done` with the model that actually ran +
        token usage + measured latency. Also mirrors into the active unified
        Trace (Phase B, 2026-05-12) when one is active.

        `input_text` / `output_text` are the preferred kwargs when callers
        already hold the relevant strings; `resp` + `user_msg` are a
        convenience for call sites that want the trace to extract text from
        the Anthropic response object directly. All four default to None for
        back-compat with legacy call sites."""
        latency_ms = 0
        try:
            latency_ms = int((time.monotonic() - started_at) * 1000)
            usage = usage or {}
            log_event(
                "anthropic", "call_done",
                call_type=call_type, model=model,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_creation=usage.get("cache_creation_input_tokens"),
                cache_read=usage.get("cache_read_input_tokens"),
                latency_ms=latency_ms,
            )
        except Exception:
            logger.debug("structured_log call_done emit failed (continuing)")
        # Spend counter hook (2026-06-10, spend_state.py). Never raises.
        try:
            from kavi_runtime.spend_state import record_spend_from_usage
            record_spend_from_usage(self._config, model, call_type, usage or {})
        except Exception:
            logger.debug("spend_state record hook failed (continuing)", exc_info=True)
        # Trace mirror (no-op if no active trace). Failure-safe: any error here
        # must not propagate; the LLM call already succeeded by this point.
        try:
            from kavi_runtime.trace_log import current as _trace_current
            _t = _trace_current()
            if _t is not None:
                usage = usage or {}
                # Derive output_text from `resp` if the caller passed it and
                # didn't pre-extract. Mirror's input_text from `user_msg` when
                # input_text wasn't supplied directly. Defensive: any failure
                # to derive falls back to None rather than raising.
                _out = output_text
                if _out is None and resp is not None:
                    try:
                        _out = "".join(
                            b.text for b in (resp.content or [])
                            if hasattr(b, "text")
                        )
                    except Exception:
                        _out = None
                _in = input_text if input_text is not None else user_msg
                _t.record_llm_call(
                    call_type=call_type,
                    model=model,
                    input_text=_in,
                    output_text=_out,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                    cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                    latency_ms=latency_ms,
                )
        except Exception as _trace_err:
            logger.debug("claude_client: trace mirror failed (continuing): %s", _trace_err)

    def _log_call_failed(self, call_type: str, started_at: float,
                          exc: BaseException, retries: int = 0) -> None:
        """Emit `anthropic.call_failed` with error class + retry count +
        latency on failure path."""
        try:
            latency_ms = int((time.monotonic() - started_at) * 1000)
            log_event(
                "anthropic", "call_failed",
                call_type=call_type,
                error_class=type(exc).__name__,
                retries=retries,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.debug("structured_log call_failed emit failed (continuing)")

    def _model_for_call_type(self, call_type: str | None) -> str:
        """Resolve which Claude model to use for a given call type.

        Lookup order:
          1. `model_routing[<call_type>]` from config (per-call-type override).
          2. `model_routing.default` from config.
          3. `claude.model` from config (the legacy single-model setting).

        Defaults guarantee that any call site that forgets to pass `call_type`
        keeps using the prior single-model behavior — no regression for
        composer paths that haven't been threaded through routing.
        """
        if call_type and call_type in self._model_routing:
            return self._model_routing[call_type]
        return self._model_routing.get("default", self._model)

    def _max_tokens_for_call_type(self, call_type: str | None) -> int:
        """Resolve which max_tokens cap to use for a given call type.

        Lookup order:
          1. `max_tokens_routing[<call_type>]` from config (per-call-type override).
          2. `max_tokens_routing.default` from config.
          3. `claude.max_tokens` from config (the legacy single-cap setting).

        Mirrors `_model_for_call_type`. Today only
        `compose_email_to_tasks_judgment` is overridden (capped at 512); other
        call sites that pass max_tokens inline (e.g., `max_tokens=300` on the
        composer paths) bypass this routing entirely and keep their inline cap.
        """
        if call_type and call_type in self._max_tokens_routing:
            return self._max_tokens_routing[call_type]
        return self._max_tokens_routing.get("default", self._max_tokens)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Parse JSON from the model's response. Tolerates ```json fences,
        opening-only fences (no close — common when a token cap clips the tail),
        and JSON wrapped in surrounding prose.
        """
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*\n?", "", text, count=1)
        text = re.sub(r"\n?\s*```\s*$", "", text, count=1)
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            first = text.find("{")
            last = text.rfind("}")
            if first != -1 and last > first:
                try:
                    return json.loads(text[first:last + 1])
                except json.JSONDecodeError:
                    return None
            return None
    # check_semantic_duplicate resolves via _CAPABILITY_DISPATCH (the explicit
    # proxy method was redundant with the dispatch-table entry; removed
    # 2026-06-10 to stay under the monolith line cap).
