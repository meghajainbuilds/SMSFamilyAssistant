"""Tests for the Phase B unified-trace surface (`kavi_runtime.trace_log`).

Covers the `Trace` context manager:
  - record_inbound / record_llm_call / record_tool_call / record_outbound /
    record_system_prompt / record_context all populate the trace
  - __exit__ writes exactly one JSONL row to the configured exchanges path
  - record_system_prompt is idempotent (first call wins)
  - exceptions inside record_* methods are swallowed
  - exchange_outcome defaults are derived (outbound_sent / skipped_silently /
    errored) when set_exchange_outcome wasn't called
  - nested Trace blocks are handled gracefully (inner wins for current())
  - current() returns None outside a Trace block

No LLM, no network, no BlueBubbles. Uses tmp_path for the JSONL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kavi_runtime.trace_log import Trace, current, current_trace


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _config(tmp_path: Path) -> dict:
    return {"paths": {"exchanges_jsonl": str(tmp_path / "exchanges.jsonl")}}


# ---- basic lifecycle -----------------------------------------------------


def test_enter_exit_writes_one_row(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", participant="+15555550101", config=cfg) as t:
        t.record_inbound(text="hi", sender="+15555550101", source="imessage")
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert len(rows) == 1
    row = rows[0]
    assert row["channel"] == "imessage"
    assert row["participant"] == "+15555550101"
    assert row["inbound"]["text"] == "hi"
    assert row["inbound"]["char_count"] == 2
    assert row["trace_id"].startswith("x_")
    assert row["exchange_outcome"] == "skipped_silently"  # no outbound


def test_current_returns_active_trace(tmp_path):
    cfg = _config(tmp_path)
    assert current() is None
    with Trace(channel="imessage", config=cfg) as t:
        assert current() is t
        # The contextvar accessor returns the same trace.
        assert current_trace.get() is t
    assert current() is None


# ---- record_* methods ----------------------------------------------------


def test_record_llm_call_appends(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as t:
        t.record_llm_call(
            call_type="classify_correction",
            model="claude-sonnet-4-6",
            input_text="user says: this is wrong",
            output_text='{"status": "correction"}',
            input_tokens=100,
            output_tokens=20,
            latency_ms=750,
        )
        t.record_llm_call(
            call_type="compose_conversational",
            model="claude-sonnet-4-6",
            input_text="reply",
            output_text="ok",
            input_tokens=50,
            output_tokens=10,
        )
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert len(rows) == 1
    calls = rows[0]["llm_calls"]
    assert len(calls) == 2
    assert calls[0]["call_type"] == "classify_correction"
    assert calls[0]["input_text"] == "user says: this is wrong"
    assert calls[0]["latency_ms"] == 750
    assert calls[1]["call_type"] == "compose_conversational"


def test_record_tool_call_appends(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as t:
        t.record_tool_call(
            "graph.update_todo_task",
            {"task_id": "abc", "patch": {"status": "completed"}},
            {"ok": True},
            duration_ms=320,
        )
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    tools = rows[0]["tool_calls"]
    assert len(tools) == 1
    assert tools[0]["tool"] == "graph.update_todo_task"
    assert tools[0]["args"]["task_id"] == "abc"
    assert tools[0]["result"]["ok"] is True
    assert tools[0]["duration_ms"] == 320


def test_record_outbound_appends_and_marks_outcome(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as t:
        t.record_outbound(
            decision_id="p_xyz_conversational_abcd1234",
            kind="conversational",
            text="Got it.",
            structural_checks={"G-V1": True},
            structural_pass=True,
            fallback_used=False,
            verified=True,
        )
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    out = rows[0]["outbound"]
    assert len(out) == 1
    assert out[0]["decision_id"] == "p_xyz_conversational_abcd1234"
    assert out[0]["text"] == "Got it."
    assert out[0]["char_count"] == 7
    # Default outcome derives from non-empty outbound.
    assert rows[0]["exchange_outcome"] == "outbound_sent"


def test_record_system_prompt_snapshots_and_hashes(tmp_path):
    cfg = _config(tmp_path)
    prompt = "# Skill\n\nyou are Kavi.\n# Household\n..."
    with Trace(channel="imessage", config=cfg) as t:
        t.record_system_prompt(prompt)
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    cad = rows[0]["context_at_decision"]
    assert cad["system_prompt_text"] == prompt
    assert cad["system_prompt_hash"].startswith("sha256:")
    # SHA256 hex digest length is 64.
    assert len(cad["system_prompt_hash"]) == len("sha256:") + 64


def test_record_system_prompt_idempotent_first_wins(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as t:
        t.record_system_prompt("first prompt")
        t.record_system_prompt("second prompt (should be ignored)")
        t.record_system_prompt(None)  # also ignored
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert rows[0]["context_at_decision"]["system_prompt_text"] == "first prompt"


def test_record_context_merges_fields(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as t:
        t.record_context(corrections_injected=[{"id": 1}])
        t.record_context(durable_facts_active=[{"f": "x"}])
        t.record_context(open_tasks_top_n=[{"id": "t1"}])
        t.record_context(skill_files_loaded=["kavi_conversation"])
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    cad = rows[0]["context_at_decision"]
    assert cad["corrections_injected"] == [{"id": 1}]
    assert cad["durable_facts_active"] == [{"f": "x"}]
    assert cad["open_tasks_top_n"] == [{"id": "t1"}]
    assert cad["skill_files_loaded"] == ["kavi_conversation"]


# ---- exchange_outcome derivation -----------------------------------------


def test_exchange_outcome_explicit_overrides_default(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as t:
        t.set_exchange_outcome("paused")
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert rows[0]["exchange_outcome"] == "paused"


def test_exchange_outcome_errored_on_exception(tmp_path):
    cfg = _config(tmp_path)
    with pytest.raises(RuntimeError):
        with Trace(channel="imessage", config=cfg):
            raise RuntimeError("boom")
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert len(rows) == 1
    assert rows[0]["exchange_outcome"] == "errored"


# ---- failure-safety ------------------------------------------------------


def test_record_methods_swallow_exceptions(tmp_path, monkeypatch):
    """Each record_* method must swallow internal exceptions. Force one by
    monkeypatching the internal storage to a non-list / non-dict so .append /
    assignment misbehaves; the Trace context manager must still exit cleanly.
    """
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as t:
        # Corrupt internal state to force AttributeError on next append.
        t.llm_calls = None  # type: ignore[assignment]
        t.tool_calls = None  # type: ignore[assignment]
        t.outbound = None  # type: ignore[assignment]
        # None of these should raise.
        t.record_llm_call(call_type="x", model="y")
        t.record_tool_call("t", {}, {})
        t.record_outbound(kind="conversational", text="x")
    # __exit__ ran; row was still written (with corrupted None fields).
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert len(rows) == 1


def test_exit_write_failure_does_not_raise(tmp_path):
    """If the JSONL path is unwritable, __exit__ logs and continues."""
    # Point exchanges_jsonl at a path whose parent cannot be created.
    bad = "/dev/null/no-such-dir/exchanges.jsonl"
    cfg = {"paths": {"exchanges_jsonl": bad}}
    # Should not raise.
    with Trace(channel="imessage", config=cfg) as t:
        t.record_inbound(text="hi")
    # No assertion needed beyond "did not raise."


def test_falls_back_to_default_path_when_config_missing_key(tmp_path, monkeypatch):
    """When config lacks `paths.exchanges_jsonl`, the module uses the default
    Kavi path. We don't want a real write on the test box, so monkeypatch the
    module-level default."""
    fake_default = tmp_path / "default-exchanges.jsonl"
    monkeypatch.setattr(
        "kavi_runtime.trace_log._DEFAULT_EXCHANGES_PATH", str(fake_default),
    )
    cfg = {"paths": {}}  # missing exchanges_jsonl
    with Trace(channel="imessage", config=cfg):
        pass
    assert fake_default.exists()


# ---- nesting -------------------------------------------------------------


def test_nested_trace_inner_wins_for_current(tmp_path):
    """Nested Trace blocks each write their own row; current() returns the
    inner-most trace inside the inner block; the outer trace is restored on
    inner __exit__."""
    cfg = _config(tmp_path)
    with Trace(channel="imessage", config=cfg) as outer:
        assert current() is outer
        with Trace(channel="email", config=cfg, capability="inbox-to-task") as inner:
            assert current() is inner
            inner.record_inbound(text="inner")
        # Outer restored.
        assert current() is outer
        outer.record_inbound(text="outer")
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert len(rows) == 2
    # Order: inner __exit__ wrote first, then outer.
    assert rows[0]["inbound"]["text"] == "inner"
    assert rows[0]["channel"] == "email"
    assert rows[1]["inbound"]["text"] == "outer"
    assert rows[1]["channel"] == "imessage"


# ---- full row shape ------------------------------------------------------


def test_full_row_has_all_expected_keys(tmp_path):
    cfg = _config(tmp_path)
    with Trace(channel="imessage", participant="+1", config=cfg) as t:
        t.record_inbound(text="hi", sender="+1", source="imessage")
        t.record_system_prompt("system")
        t.record_context(corrections_injected=[], durable_facts_active=[])
        t.record_llm_call(call_type="x", model="y", input_text="a", output_text="b")
        t.record_tool_call("graph.list_open_todo_tasks", {}, {"ok": True})
        t.record_outbound(decision_id="d1", kind="conversational", text="ok")
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    row = rows[0]
    for key in (
        "trace_id", "thread_id", "timestamp", "channel", "capability",
        "participant", "inbound", "context_at_decision", "llm_calls",
        "tool_calls", "outbound", "exchange_outcome",
    ):
        assert key in row, f"missing key: {key}"
    for cad_key in (
        "system_prompt_hash", "system_prompt_text", "skill_files_loaded",
        "corrections_injected", "durable_facts_active", "open_tasks_top_n",
    ):
        assert cad_key in row["context_at_decision"]


# ---- insertion integration smoke ----------------------------------------


def test_outbound_log_mirrors_into_active_trace(tmp_path):
    """outbound_log.log_outbound is the insertion point for the outbound
    surface. With a Trace active, calling log_outbound must populate the
    trace's `outbound[]` AND continue writing to the per-capability
    eval-persona-outbound-judgments.jsonl."""
    from kavi_runtime.outbound_log import log_outbound

    eval_path = tmp_path / "eval-persona-outbound.jsonl"
    cfg = {
        "paths": {
            "exchanges_jsonl": str(tmp_path / "exchanges.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(eval_path),
        }
    }
    with Trace(channel="imessage", config=cfg) as t:
        decision_id = log_outbound(
            cfg, kind="conversational", text="Got it.",
            send_result={"verified": True, "fallback_used": False},
        )
        assert decision_id  # not empty
        # Trace recorded the outbound inline.
        assert len(t.outbound) == 1
        assert t.outbound[0]["decision_id"] == decision_id
    # Both files have one row.
    assert len(_read_jsonl(eval_path)) == 1
    rows = _read_jsonl(Path(cfg["paths"]["exchanges_jsonl"]))
    assert len(rows) == 1
    assert rows[0]["outbound"][0]["decision_id"] == decision_id


# ---- 2026-09-23: email exchanges record what happened to the email ---------


import pytest as _pytest


@_pytest.mark.parametrize("result,expected", [
    ({"ts": "t", "message_id": "m", "result": {}, "task_id": "T1"}, "task_created"),
    ({"ts": "t", "message_id": "m", "task_id": "T1", "lifecycle_state": "delivered"}, "task_updated"),
    ({"ts": "t", "message_id": "m", "result": {}, "task_id": "T1", "dedup": "graph"}, "dedup_hit"),
    ({"status": "webhook_redup_hit"}, "webhook_redup_hit"),
    ({"status": "paused_skipped"}, "paused"),
    ({"status": "skipped", "reason": "non_inbox_folder"}, "skipped"),
    ({"ts": "t", "message_id": "m", "result": {"status": "skipped"}}, "skipped"),
    ({"ts": "t", "message_id": "m", "result": {}, "error": "todo_create_failed"}, "errored"),
])
def test_email_outcome_mapping(result, expected) -> None:
    from capabilities.inbox_to_task.handler import email_outcome
    assert email_outcome(result) == expected


def test_task_created_without_imessage_is_not_skipped_silently(tmp_path, monkeypatch) -> None:
    """Old Navy, 2026-09-23 14:06 PT: a task was created with no iMessage and
    the trace said skipped_silently."""
    import json
    from kavi_runtime.trace_log import Trace
    from capabilities.inbox_to_task import handler
    cfg = {"paths": {"exchanges_jsonl": str(tmp_path / "x.jsonl"),
                     "imessage_state": str(tmp_path / "s.json")}}
    monkeypatch.setattr(handler, "_email_arrived_body",
                        lambda n, c, a=None: {"ts": "t", "message_id": "m", "result": {}, "task_id": "T1"})
    with Trace(channel="email", participant=None, config=cfg, capability="inbox-to-task") as t:
        handler._email_arrived_impl({"resourceData": {"id": "OLDNAVY-1"}}, cfg)
    assert t._exchange_outcome == "task_created"
