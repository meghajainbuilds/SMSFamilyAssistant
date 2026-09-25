"""Replay past email judgments through two models and measure agreement.

Why this script exists
======================
The email judge (compose_email_to_tasks_judgment) is the biggest remaining
line on Kavi's bill. Before moving it from Sonnet to Haiku we need proof
that Haiku makes the same task-or-skip call. Ship gate (handoff 2026-09-23):
>= 95% agreement on task-vs-skip, and Megha labels the disagreements.

Both models run on TODAY's system prompt with the exact user message each
email was originally judged on (stored in evals/traces/exchanges.jsonl).
The recorded historical decision is NOT the baseline: most of it was made
on older prompts, so comparing against it would blame Haiku for prompt
drift.

Runs through the Message Batches API (50% price, outside Kavi's spend
counter). Never writes to MS To Do, never sends a message.

CLI (on Kavi's Mac, from ~/kavi-runtime)
========================================
    .venv/bin/python -m scripts.replay_email_judge sample --n 500
    .venv/bin/python -m scripts.replay_email_judge submit
    .venv/bin/python -m scripts.replay_email_judge collect   # re-run until "ended"
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(Path.home() / ".config" / "kavi" / ".env")

from anthropic.types.message_create_params import MessageCreateParamsNonStreaming  # noqa: E402
from anthropic.types.messages.batch_create_params import Request  # noqa: E402

from capabilities.inbox_to_task.compose import JUDGMENT_SCHEMA  # noqa: E402
from kavi_runtime.claude_client import ClaudeClient  # noqa: E402

CALL_TYPE = "compose_email_to_tasks_judgment"
MODELS = {"sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5", "sonnet5": "claude-sonnet-5"}
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
EXCHANGES = Path("/Users/kavi/HomeOS/evals/traces/exchanges.jsonl")
OUT = Path("/Users/kavi/HomeOS/evals/inbox-to-task/haiku-replay")
# Run 2 (2026-09-24): production request shape (structured output, reason
# before status, 1024 cap). Separate folder so run 1 stays intact.
RUN = OUT / "run2-structured"


def _client() -> ClaudeClient:
    with open(_CONFIG_PATH) as f:
        return ClaudeClient(yaml.safe_load(f))


def _decision(output_text: str) -> dict:
    """Normalize a judge output to {decision, owner, title}."""
    parsed = ClaudeClient._extract_json(output_text or "")
    if parsed is None:
        return {"decision": "unparsed", "owner": None, "title": None}
    if parsed.get("status") == "skipped":
        return {"decision": "skip", "owner": None, "title": None,
                "reason": parsed.get("reason")}
    task = parsed.get("task") if isinstance(parsed.get("task"), dict) else parsed
    return {"decision": "task", "owner": task.get("owner"), "title": task.get("title"),
            "confidence": task.get("confidence")}


def sample(n: int, seed: int) -> None:
    by_email: dict[str, dict] = {}
    with EXCHANGES.open() as f:
        for line in f:
            if CALL_TYPE not in line:
                continue
            d = json.loads(line)
            calls = [c for c in d.get("llm_calls", [])
                     if c.get("call_type") == CALL_TYPE and c.get("input_text")]
            if not calls:
                continue
            eid = (d.get("inbound") or {}).get("inbound_id") or d["trace_id"]
            by_email[eid] = {  # latest judgment per email wins
                "email_id": eid,
                "ts": d["timestamp"],
                "account": d.get("participant"),
                "user_msg": calls[-1]["input_text"],
                "historical": _decision(calls[-1].get("output_text")),
            }
    rows = list(by_email.values())
    tasks = [r for r in rows if r["historical"]["decision"] == "task"]
    skips = [r for r in rows if r["historical"]["decision"] == "skip"]
    rng = random.Random(seed)
    # Half and half, so rare-but-costly task decisions get real coverage.
    picked = rng.sample(tasks, min(n // 2, len(tasks)))
    picked += rng.sample(skips, min(n - len(picked), len(skips)))
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "sample.jsonl").open("w") as f:
        for i, r in enumerate(sorted(picked, key=lambda x: x["ts"])):
            r["case_id"] = f"c{i:04d}"
            f.write(json.dumps(r) + "\n")
    print(f"pool={len(rows)} (task={len(tasks)} skip={len(skips)}) sampled={len(picked)}")


def submit() -> None:
    client = _client()
    system = client._build_system_prompt(
        "email_to_tasks", cache_ttl="1h", with_persona=False, with_capability_doc=True,
    )
    max_toks = client._max_tokens_for_call_type(CALL_TYPE)
    cases = [json.loads(l) for l in (OUT / "sample.jsonl").open()]
    requests = [
        Request(
            custom_id=f"{case['case_id']}-{tag}",
            params=MessageCreateParamsNonStreaming(
                model=model, max_tokens=max_toks, system=system,
                messages=[{"role": "user", "content": case["user_msg"]}],
                output_config={"format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA}},
            ),
        )
        for tag, model in MODELS.items()
        for case in cases
    ]
    batch = client._anthropic.messages.batches.create(requests=requests)
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / "batch.json").write_text(json.dumps({"batch_id": batch.id, "n_requests": len(requests)}))
    print(f"batch={batch.id} requests={len(requests)} status={batch.processing_status}")


# Batch = 50% of list price. $/1M tokens: input, output, cache write (1h = 2x), cache read.
_PRICE = {
    "claude-sonnet-4-6": (1.5, 7.5, 3.0, 0.15),
    "claude-haiku-4-5": (0.5, 2.5, 1.0, 0.05),
    "claude-sonnet-5": (1.0, 5.0, 2.0, 0.10),
}


def _payload(case: dict) -> dict:
    m = re.search(r"```json\n(.*)\n```\s*$", case["user_msg"], re.S)
    return json.loads(m.group(1)) if m else {}


def collect() -> None:
    """Run 2: score each candidate against the production baseline
    (Sonnet 4.6, same request shape). Also simulates the routed option:
    Haiku by default, Sonnet 4.6 for emails a keep rule protects."""
    from kavi_runtime.inbox_pre_filter import keep_rule

    client = _client()
    with open(_CONFIG_PATH) as f:
        keep = (yaml.safe_load(f).get("inbox_pre_filter") or {}).get("keep")
    batch_id = json.loads((RUN / "batch.json").read_text())["batch_id"]
    batch = client._anthropic.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        print(f"status={batch.processing_status} counts={batch.request_counts}")
        sys.exit(2)

    outputs: dict[str, dict] = {}
    cost = {m: 0.0 for m in MODELS.values()}
    for res in client._anthropic.messages.batches.results(batch_id):
        case_id, tag = res.custom_id.rsplit("-", 1)
        if res.result.type != "succeeded":
            outputs.setdefault(case_id, {})[tag] = {"decision": f"error:{res.result.type}"}
            continue
        msg = res.result.message
        text = "".join(b.text for b in msg.content if b.type == "text")
        d = _decision(text) if msg.stop_reason == "end_turn" else {"decision": f"stop:{msg.stop_reason}"}
        outputs.setdefault(case_id, {})[tag] = {**d, "raw": text}
        pr, u = _PRICE[MODELS[tag]], msg.usage
        cost[MODELS[tag]] += (
            u.input_tokens * pr[0] + u.output_tokens * pr[1]
            + (u.cache_creation_input_tokens or 0) * pr[2]
            + (u.cache_read_input_tokens or 0) * pr[3]
        ) / 1e6

    cases = {c["case_id"]: c for c in map(json.loads, (OUT / "sample.jsonl").open())}
    rows = []
    for cid, case in sorted(cases.items()):
        email = _payload(case).get("email") or {}
        o = outputs.get(cid, {})
        kept = keep_rule(email, keep)
        o["routed"] = o.get("sonnet", {}) if kept else o.get("haiku", {})
        rows.append({"case_id": cid, "sender": email.get("from_address"),
                     "subject": email.get("subject"), "kept_by": kept,
                     **{k: {x: v.get(x) for x in ("decision", "owner", "title", "confidence")}
                        for k, v in o.items()}})
    with (RUN / "results.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    summary = {"batch_id": batch_id, "cost_usd": {k: round(v, 2) for k, v in cost.items()},
               "baseline": "sonnet (claude-sonnet-4-6, production shape)", "candidates": {}}
    for cand in ("haiku", "sonnet5", "routed"):
        ok = [r for r in rows if r.get("sonnet", {}).get("decision") in ("task", "skip")]
        agree = sum(r[cand]["decision"] == r["sonnet"]["decision"] for r in ok)
        base_tasks = [r for r in ok if r["sonnet"]["decision"] == "task"]
        missed = [r for r in base_tasks if r[cand]["decision"] != "task"]
        extra = [r for r in ok if r["sonnet"]["decision"] == "skip" and r[cand]["decision"] == "task"]
        summary["candidates"][cand] = {
            "agreement": round(agree / len(ok), 4),
            "tasks_kept": f"{len(base_tasks) - len(missed)}/{len(base_tasks)}",
            "task_recall": round(1 - len(missed) / len(base_tasks), 4) if base_tasks else None,
            "extra_tasks": len(extra),
            "unusable": sum(r[cand]["decision"] not in ("task", "skip") for r in ok),
            "missed": [f"{r['sender']} | {r['subject']}" for r in missed][:40],
        }
    summary["routed_share_to_sonnet"] = round(sum(bool(r["kept_by"]) for r in rows) / len(rows), 3)
    (RUN / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


RUN3 = OUT / "run3-sonnet5-nothink"


def submit_sonnet5() -> None:
    """Run 3: Sonnet 5 with thinking disabled. In run 2 it thought by
    default and 122/500 replies hit the 1024 cap before answering."""
    client = _client()
    system = client._build_system_prompt(
        "email_to_tasks", cache_ttl="1h", with_persona=False, with_capability_doc=True,
    )
    cases = [json.loads(l) for l in (OUT / "sample.jsonl").open()]
    requests = [Request(custom_id=f"{c['case_id']}-sonnet5", params=MessageCreateParamsNonStreaming(
        model="claude-sonnet-5", max_tokens=client._max_tokens_for_call_type(CALL_TYPE),
        system=system, thinking={"type": "disabled"},
        messages=[{"role": "user", "content": c["user_msg"]}],
        output_config={"format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA}},
    )) for c in cases]
    batch = client._anthropic.messages.batches.create(requests=requests)
    RUN3.mkdir(parents=True, exist_ok=True)
    (RUN3 / "batch.json").write_text(json.dumps({"batch_id": batch.id}))
    print(f"batch={batch.id} requests={len(requests)}")


def collect_sonnet5() -> None:
    client = _client()
    batch_id = json.loads((RUN3 / "batch.json").read_text())["batch_id"]
    batch = client._anthropic.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        print(f"status={batch.processing_status}")
        sys.exit(2)
    new, cost = {}, 0.0
    for res in client._anthropic.messages.batches.results(batch_id):
        cid = res.custom_id.rsplit("-", 1)[0]
        if res.result.type != "succeeded":
            new[cid] = f"error:{res.result.type}"
            continue
        m = res.result.message
        text = "".join(b.text for b in m.content if b.type == "text")
        new[cid] = _decision(text)["decision"] if m.stop_reason == "end_turn" else f"stop:{m.stop_reason}"
        pr, u = _PRICE["claude-sonnet-5"], m.usage
        cost += (u.input_tokens * pr[0] + u.output_tokens * pr[1]
                 + (u.cache_creation_input_tokens or 0) * pr[2]
                 + (u.cache_read_input_tokens or 0) * pr[3]) / 1e6
    rows = [json.loads(l) for l in (RUN / "results.jsonl").open()]
    ok = [r for r in rows if r["sonnet"]["decision"] in ("task", "skip")]
    agree = sum(new.get(r["case_id"]) == r["sonnet"]["decision"] for r in ok)
    base_tasks = [r for r in ok if r["sonnet"]["decision"] == "task"]
    missed = [r for r in base_tasks if new.get(r["case_id"]) != "task"]
    summary = {"batch_id": batch_id, "cost_usd": round(cost, 2),
               "agreement": round(agree / len(ok), 4),
               "tasks_kept": f"{len(base_tasks) - len(missed)}/{len(base_tasks)}",
               "extra_tasks": sum(r["sonnet"]["decision"] == "skip" and new.get(r["case_id"]) == "task" for r in ok),
               "unusable": sum(new.get(r["case_id"]) not in ("task", "skip") for r in ok),
               "missed": [f"{r['sender']} | {r['subject']}" for r in missed]}
    (RUN3 / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "missed"}))


HOLDOUT = OUT / "holdout"


def sample_holdout(n: int, seed: int) -> None:
    """Fresh emails the routing rules were NOT designed on: same pool,
    excluding every email in sample.jsonl. Natural mix (not 50/50), so
    the result reflects real traffic."""
    used = {json.loads(l)["email_id"] for l in (OUT / "sample.jsonl").open()}
    by_email: dict[str, dict] = {}
    with EXCHANGES.open() as f:
        for line in f:
            if CALL_TYPE not in line:
                continue
            d = json.loads(line)
            calls = [c for c in d.get("llm_calls", [])
                     if c.get("call_type") == CALL_TYPE and c.get("input_text")]
            eid = (d.get("inbound") or {}).get("inbound_id") or d["trace_id"]
            if calls and eid not in used:
                by_email[eid] = {"email_id": eid, "ts": d["timestamp"],
                                 "user_msg": calls[-1]["input_text"]}
    rows = random.Random(seed).sample(list(by_email.values()), min(n, len(by_email)))
    HOLDOUT.mkdir(parents=True, exist_ok=True)
    with (HOLDOUT / "sample.jsonl").open("w") as f:
        for i, r in enumerate(sorted(rows, key=lambda x: x["ts"])):
            r["case_id"] = f"h{i:04d}"
            f.write(json.dumps(r) + "\n")
    print(f"pool={len(by_email)} sampled={len(rows)}")


def submit_holdout() -> None:
    client = _client()
    system = client._build_system_prompt(
        "email_to_tasks", cache_ttl="1h", with_persona=False, with_capability_doc=True,
    )
    cases = [json.loads(l) for l in (HOLDOUT / "sample.jsonl").open()]
    requests = [Request(custom_id=f"{c['case_id']}-{tag}", params=MessageCreateParamsNonStreaming(
        model=MODELS[tag], max_tokens=client._max_tokens_for_call_type(CALL_TYPE), system=system,
        messages=[{"role": "user", "content": c["user_msg"]}],
        output_config={"format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA}},
    )) for tag in ("sonnet", "haiku") for c in cases]
    batch = client._anthropic.messages.batches.create(requests=requests)
    (HOLDOUT / "batch.json").write_text(json.dumps({"batch_id": batch.id}))
    print(f"batch={batch.id} requests={len(requests)}")


def collect_holdout() -> None:
    """Score "Haiku plus rules" on the holdout. The rules come from the
    private config (inbox_to_task.judge_routing) and were frozen before this
    ran. Baseline = Sonnet 4.6 on every email. Also reports mean tokens per
    call by model, because run 2's batch cost for Haiku looked too high."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from capabilities.inbox_to_task.routing import CHEAP, first_route, needs_second_look

    client = _client()
    cfg = yaml.safe_load(_CONFIG_PATH.read_text())
    routing = (cfg.get("inbox_to_task") or {}).get("judge_routing")
    keep = (cfg.get("inbox_pre_filter") or {}).get("keep")
    if not routing:
        sys.exit("no inbox_to_task.judge_routing in config")
    batch_id = json.loads((HOLDOUT / "batch.json").read_text())["batch_id"]
    if client._anthropic.messages.batches.retrieve(batch_id).processing_status != "ended":
        sys.exit("batch not ended")

    out: dict[str, dict] = {}
    usage = {t: {"n": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "cost": 0.0}
             for t in ("sonnet", "haiku")}
    for res in client._anthropic.messages.batches.results(batch_id):
        cid, tag = res.custom_id.rsplit("-", 1)
        if res.result.type != "succeeded":
            out.setdefault(cid, {})[tag] = {"decision": f"error:{res.result.type}", "cost": 0.0}
            continue
        m = res.result.message
        text = "".join(b.text for b in m.content if b.type == "text")
        d = _decision(text) if m.stop_reason == "end_turn" else {"decision": f"stop:{m.stop_reason}"}
        pr, u = _PRICE[MODELS[tag]], m.usage
        cw, cr = u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
        c = (u.input_tokens * pr[0] + u.output_tokens * pr[1] + cw * pr[2] + cr * pr[3]) / 1e6
        s = usage[tag]
        s["n"] += 1; s["input"] += u.input_tokens; s["cache_write"] += cw
        s["cache_read"] += cr; s["output"] += u.output_tokens; s["cost"] += c
        out.setdefault(cid, {})[tag] = {**d, "cost": c}

    cases = [json.loads(l) for l in (HOLDOUT / "sample.jsonl").open()]
    rows, routed_cost, base_cost = [], 0.0, 0.0
    counts = {"strong_first": 0, "second_look": 0}
    for case in cases:
        o = out.get(case["case_id"], {})
        son, hai = o.get("sonnet", {}), o.get("haiku", {})
        email = _payload(case).get("email") or {}
        route, why = first_route(email, routing, keep)
        if route != CHEAP:
            final, path, cost = son, f"strong:{why}", son.get("cost", 0.0)
            counts["strong_first"] += 1
        elif hai.get("decision") == "skip" and needs_second_look(email, {"status": "skipped"}, routing):
            final, path, cost = son, "second_look", hai.get("cost", 0.0) + son.get("cost", 0.0)
            counts["second_look"] += 1
        else:
            final, path, cost = hai, "cheap", hai.get("cost", 0.0)
        routed_cost += cost
        base_cost += son.get("cost", 0.0)
        rows.append({"case_id": case["case_id"], "sender": email.get("from_address"),
                     "subject": email.get("subject"), "path": path,
                     "sonnet": son.get("decision"), "haiku": hai.get("decision"),
                     "routed": final.get("decision")})
    with (HOLDOUT / "results.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    ok = [r for r in rows if r["sonnet"] in ("task", "skip")]
    base_tasks = [r for r in ok if r["sonnet"] == "task"]

    def score(key: str) -> dict:
        missed = [r for r in base_tasks if r[key] != "task"]
        return {
            "agreement": round(sum(r[key] == r["sonnet"] for r in ok) / len(ok), 4),
            "tasks_kept": f"{len(base_tasks) - len(missed)}/{len(base_tasks)}",
            "extra_tasks": sum(r["sonnet"] == "skip" and r[key] == "task" for r in ok),
            "unusable": sum(r[key] not in ("task", "skip") for r in ok),
            "missed": [f"{r['sender']} | {r['subject']} | {r['path']}" for r in missed],
        }

    summary = {
        "batch_id": batch_id, "cases": len(rows), "baseline_tasks": len(base_tasks),
        "haiku_alone": score("haiku"), "haiku_plus_rules": score("routed"),
        "share_strong_first": round(counts["strong_first"] / len(rows), 3),
        "share_second_look": round(counts["second_look"] / len(rows), 3),
        "cost_all_sonnet_usd": round(base_cost, 2), "cost_routed_usd": round(routed_cost, 2),
        "saving_vs_all_sonnet": round(1 - routed_cost / base_cost, 3) if base_cost else None,
        "mean_tokens_per_call": {t: {k: round(v / s["n"]) for k, v in s.items() if k not in ("n", "cost")}
                                 for t, s in usage.items() if s["n"]},
    }
    (HOLDOUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items()}, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample")
    sp.add_argument("--n", type=int, default=500)
    sp.add_argument("--seed", type=int, default=42)
    sub.add_parser("submit")
    sub.add_parser("collect")
    sub.add_parser("submit-sonnet5")
    sub.add_parser("sample-holdout")
    sub.add_parser("submit-holdout")
    sub.add_parser("collect-sonnet5")
    sub.add_parser("collect-holdout")
    a = ap.parse_args()
    {"sample": lambda: sample(a.n, a.seed), "submit": submit, "collect": collect,
     "submit-sonnet5": submit_sonnet5,
     "sample-holdout": lambda: sample_holdout(500, 20260924), "submit-holdout": submit_holdout, "collect-sonnet5": collect_sonnet5,
     "collect-holdout": collect_holdout}[a.cmd]()


if __name__ == "__main__":
    main()
