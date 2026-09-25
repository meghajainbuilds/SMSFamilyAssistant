"""System-prompt + voice-capped-call helpers for ClaudeClient.

Phase 4 (2026-06-02) physical move from kavi_runtime/claude_client.py.
These functions are cross-cutting infrastructure (used by every capability
composer) — they own no capability-specific behavior, only the prompt
assembly + cache-control plumbing + voice-cap response shape.

Each function takes `client: ClaudeClient` as first arg (replacing `self`).
Wired into ClaudeClient via thin method wrappers in claude_client.py.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from kavi_runtime.claude_client import ClaudeClient

from kavi_runtime.persona_loader import load_persona_text
from kavi_runtime.private_overlay import PrivateOverlayError, read_resolved
from kavi_runtime.security_baseline import load_security_baseline_text

logger = logging.getLogger(__name__)


def _est_input_tokens(system: list, user_msg: str) -> int:
    total = sum(len(b.get("text", "")) for b in system)
    total += len(user_msg)
    return int(total / 4)


def _skill(client, name: str) -> str:
    # Skill-override hook (2026-06-24, prompt-optimizer closed loop): a verify
    # replay may inject a candidate skill text via `client._skill_overrides`
    # ({skill_name: text}) so the optimizer can MEASURE a prompt variant's
    # graded score on staging instead of only ranking it. Synthetic-replay
    # only — the override is per-request, set+cleared by the verify route, and
    # absent in normal operation.
    overrides = getattr(client, "_skill_overrides", None)
    if overrides and name in overrides:
        return overrides[name]
    return read_resolved(client._skills_dir / f"{name}.md")


def apply_skill_override(client, payload) -> None:
    """Set per-request skill overrides from a synthetic verify payload's
    `skill_override` ({skill_name: text}). No-op when absent or malformed.
    This is what lets the prompt-optimizer MEASURE a candidate skill's graded
    score on staging (the verify route loads the override instead of the
    on-disk skill via `_skill` above). Synthetic-replay only."""
    so = payload.get("skill_override") if isinstance(payload, dict) else None
    if isinstance(so, dict):
        clean = {k: v for k, v in so.items() if isinstance(k, str) and isinstance(v, str)}
        if clean:
            client._skill_overrides = clean


def _household(client) -> str:
    return client._household_md_path.read_text()


_BEHAVIOR_H2 = re.compile(r"^## Behavior\b.*$", re.MULTILINE)


def _inbox_to_task(client) -> str | None:
    """Behavior section (Hard rules / Examples / Q&A learned) of the
    inbox-to-task capability doc. Optional: returns None if not configured or
    file missing. Logged-and-skipped on read failure so a transient FS hiccup
    doesn't crash email_arrived.

    Behavior section only (2026-09-23 cost pass): the doc's own prompt-cache
    spec says the prefix carries "this capability's Behavior section", but the
    whole file (TL;DR, Metrics, Architecture, Changelog; ~18k tokens live) was
    being sent on every email judgment. Falls back to the full text if the doc
    has no Behavior heading, so a restructured doc degrades to old behavior
    rather than to no rules."""
    if not client._inbox_to_task_md_path:
        return None
    try:
        text = read_resolved(client._inbox_to_task_md_path)
    except PrivateOverlayError:
        # Never judge email without the family's real rules; fail loudly.
        raise
    except Exception as e:
        logger.warning("inbox_to_task_md read failed (continuing without): %s", e)
        return None
    from kavi_runtime.persona_loader import _extract_section
    return _extract_section(text, _BEHAVIOR_H2) or text


def _persona(client) -> str | None:
    """Persona text (Behavior + System prompt sections of
    `capabilities/kavi-persona.md`). Returns None if no path was
    configured (back-compat with tests / configs predating the spec
    collapse). On read failure raises — the spec is the runtime
    persona, so a missing or malformed file is a fatal error rather
    than a silent voiceless fallback."""
    if not client._persona_md_path:
        return None
    return load_persona_text(client._persona_md_path)


def _security_baseline(client) -> str | None:
    """Security baseline text (System prompt section of
    `capabilities/security-baseline.md`). Returns None if no path
    was configured (back-compat with tests / configs predating the
    2026-05-28 collapse). On read failure raises — the spec is the
    runtime security text, so a missing or malformed file is a
    fatal error rather than a silent unsafe fallback."""
    if not client._security_baseline_md_path:
        return None
    return load_security_baseline_text(client._security_baseline_md_path)


def _build_system_prompt(
    client, skill_name: str, *, with_refusal_layer: bool = True,
    cache_ttl: str = "5m", with_persona: bool | None = None,
    with_capability_doc: bool = False,
) -> list[dict[str, Any]]:
    """Returns a content-block list with cache_control on the stable prefix.

    Order: skill prose + household.md + capabilities/inbox-to-task.md (all stable),
    cached as one block. Per-call data goes in the user message, never in the
    system prompt. The inbox-to-task.md addition (2026-04-29 evening) is the
    canonical source for Hard rules, Examples, and Q&A learned patterns; the
    skill's embedded few-shot examples are illustrative and any conflict resolves
    in favor of inbox-to-task.md.

    Persona refusal layer (added 2026-05-05, relocated 2026-05-28): unless
    `with_refusal_layer` is explicitly set to False, the security threat-
    model rules are appended. The text now loads from the canonical spec
    at `capabilities/security-baseline.md` via
    `kavi_runtime.security_baseline.load_security_baseline_text`,
    replacing the prior `PERSONA_REFUSAL_LAYER` constant. Every composer
    call inherits the refusal layer; classifier-only call sites opt out
    by passing `with_refusal_layer=False`. The text is constant across
    calls so the cache prefix stays stable; spec edits land on the next
    process restart.

    Cache TTL (added 2026-05-06 per audits/token_optimization_2026-05-06.md
    REC-2): Anthropic's prompt cache exposes "5m" (default ephemeral, 1.25x
    input price on writes) and "1h" (2x input price on writes, but reads
    stay flat). For inbox-to-task the median webhook inter-arrival is 75s
    but 40% of arrivals fall in gaps > 5 min, so the 5m TTL was being
    rewritten on roughly 4 of every 10 emails. `cache_ttl="1h"` collapses
    that re-write churn. Default stays "5m" so other call sites are
    unchanged.

    Persona spec injection (added 2026-05-27): the persona text (voice
    rules, three character traits, refusal language, identity) is now
    loaded at runtime from `capabilities/kavi-persona.md` via
    `persona_loader.load_persona_text` instead of being copy-pasted into
    each skill file. When `with_refusal_layer` is True (every Kavi-voiced
    composer), the persona block is appended BEFORE the security refusal
    layer so the LLM sees voice/identity first, then the hard security
    boundaries. Classifier-only call sites still opt out via
    `with_refusal_layer=False` — they don't need persona text because
    they don't compose voiced output.

    Opt-in blocks (2026-09-23 cost pass, docs/cost-story.md):
    - `with_capability_doc`: the inbox-to-task Behavior section is loaded only
      by call sites that judge email (email_to_tasks, correction_classifier).
      Summaries, replies, and Q&A composers were each carrying 20-30k tokens
      of email rules they never used.
    - `with_persona`: defaults to `with_refusal_layer`. A JSON-only judge that
      reads untrusted email sets `with_persona=False` to keep the security
      layer (prompt-injection defense) while dropping the voice spec it never
      speaks in.
    """
    if with_persona is None:
        with_persona = with_refusal_layer
    parts = [f"# Skill\n\n{client._skill(skill_name)}", f"# Household context\n\n{client._household()}"]
    itt = client._inbox_to_task() if with_capability_doc else None
    if itt:
        parts.append(f"# Capability behavior (canonical: Hard rules + Examples + Q&A learned)\n\n{itt}")
    if with_persona:
        persona = client._persona()
        if persona:
            parts.append(f"# Persona (canonical: capabilities/kavi-persona.md)\n\n{persona}")
    if with_refusal_layer:
        security = client._security_baseline()
        if security:
            parts.append(security)
    prefix = "\n\n".join(parts)
    block: dict[str, Any] = {"type": "text", "text": prefix}
    if client._caching:
        cc: dict[str, Any] = {"type": "ephemeral"}
        if cache_ttl and cache_ttl != "5m":
            cc["ttl"] = cache_ttl
        block["cache_control"] = cc
    # Trace mirror: snapshot the system prompt once per active trace
    # (Phase B, 2026-05-12). No-op when no trace is active or when the
    # prompt was already snapshot earlier in the same exchange.
    try:
        from kavi_runtime.trace_log import current as _trace_current
        _t = _trace_current()
        if _t is not None:
            _t.record_system_prompt(prefix)
            _t.record_context(skill_files_loaded=[skill_name])
    except Exception as _trace_err:
        logger.debug("claude_client: trace system-prompt mirror failed: %s", _trace_err)
    return [block]


def _persona_system_block(client, system_text: str) -> list[dict[str, Any]]:
    """Wrap a freeform `system_text` (used by composers that don't load a
    skill file via `_build_system_prompt`) with the persona refusal layer
    appended. Returns the cache-eligible content-block list shape that
    every Anthropic call expects.

    Use this for composers that build their system prompt inline (e.g.,
    coordination-addressee message composer, periodic summary fallback,
    weekly client-check) so they inherit the same security boundaries as
    the skill-loaded composers without forking the refusal text.

    Persona injection (added 2026-05-27): same shape as
    `_build_system_prompt` — persona block from the spec is appended
    before the refusal layer when configured. Inline-built composers
    get the same voice/identity collapse as skill-loaded ones.
    """
    persona = client._persona()
    security = client._security_baseline() or ""
    if persona:
        prefix = f"{system_text}\n\n# Persona (canonical: capabilities/kavi-persona.md)\n\n{persona}\n\n{security}"
    else:
        prefix = f"{system_text}\n\n{security}"
    block: dict[str, Any] = {"type": "text", "text": prefix}
    if client._caching:
        block["cache_control"] = {"type": "ephemeral"}
    # Trace mirror (Phase B). Snapshot the inline-built system prompt.
    try:
        from kavi_runtime.trace_log import current as _trace_current
        _t = _trace_current()
        if _t is not None:
            _t.record_system_prompt(prefix)
    except Exception as _trace_err:
        logger.debug("claude_client: trace inline system-prompt mirror failed: %s", _trace_err)
    return [block]


def _voice_capped_call(
    client,
    user_msg: str,
    system: list[dict[str, Any]],
    debug_label: str,
    max_tokens: int = 300,
    char_cap: int = 180,
    call_type: str | None = None,
) -> dict[str, Any]:
    """Helper: run a single composer call and return a dict with text +
    char_count + usage. Used by `compose_coordination_ack` to share
    plumbing with the persona-side composers without forking yet
    another method. Not used by the addressee / outcome composers
    because those have their own JSON shape requirements (attribution
    flag, REFUSED_PERSONA_CATEGORICAL handling).

    `call_type` (optional) drives both model routing and the structured
    log row. Defaults to `debug_label` so the helper still emits a
    sensible call_type when the caller doesn't pass one."""
    ct = call_type or debug_label
    fallback = {"text": None, "char_count": 0, "_usage": None}
    model = client._model_for_call_type(ct)
    started = client._log_call_start(
        ct, model, _est_input_tokens(system, user_msg),
    )
    try:
        resp = client._anthropic.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        client._log_call_failed(ct, started, e)
        logger.exception("%s API call failed: %s", debug_label, e)
        return {**fallback, "_error": f"api_error: {e}"}

    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    client._log_call_done(ct, model, started, usage,
                         input_text=user_msg, output_text=text)
    logger.info("%s usage: %s", debug_label, usage)

    parsed = client._extract_json(text)
    if not parsed or "message" not in parsed:
        logger.warning("%s: bad output %r", debug_label, text[:200])
        return {**fallback, "_usage": usage, "_error": "parse_error"}
    msg = (parsed.get("message") or "").strip()
    if not msg:
        return {**fallback, "_usage": usage, "_error": "empty_message"}
    if len(msg) > char_cap:
        logger.warning("%s: over-length %d chars; truncating", debug_label, len(msg))
        msg = msg[:char_cap]
    return {"text": msg, "char_count": len(msg), "_usage": usage}

