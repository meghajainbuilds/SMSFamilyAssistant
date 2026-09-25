#!/usr/bin/env python3
"""optimize_prompt.py — Phase 3 of the doc-driven eval loop: the
prompt-improvement engine ("improves on its own").

Given a skill (the BEHAVIOR-axis prompt for an LLM composer), a HELD-OUT
improvement set, and the Phase-1 graded scorer, this tool:

  A. SCORES the current skill on the held-out set (mean pass-rate baseline),
     reusing run_matrix._score_one_case so the number is the same one the
     scorecard climbs.
  B. ASKS an LLM (injectable client) to propose N variant skill texts that
     better satisfy the held-out cases WITHOUT touching the voice or
     structural axes (the three-axis contract is passed as context, plus
     the cases the current skill is weakest on).
  C. WRITES each variant to evals/<cap>/optimizer/variant-*.md and a ranked
     report optimizer-report.json.
  D. RECOMMENDS a winner + the EXACT staging step to MEASURE it for real.

Honest limitation — the hot-swap gap
-------------------------------------
The deployed `/synthetic/verify/<capability>` endpoint loads the skill text
that is checked out ON KAVI; this script cannot hot-swap that text from the
outside. So a variant's TRUE pass-rate is UNMEASURED until it is deployed to
staging and re-scored. This tool is honest about that:

  - The baseline IS measured (it scores the live current skill).
  - Each variant carries a `predicted_improvement` (the LLM's own claim) and
    a `measured: false` flag. No variant is ever labeled with a measured
    score it did not earn.
  - The recommended winner is the LLM's top-ranked candidate; the report's
    `next_step` is the concrete deploy-and-measure command an engineer runs
    to turn the prediction into a measured fact.

If a future endpoint accepts a `skill_override` in the verify payload, set
`--skill-override-supported` and the tool will additionally MEASURE each
variant by re-scoring with the override injected (and flip `measured: true`
for those rows). As of this writing the endpoint does NOT accept an override
(verify functions in capabilities/<cap>/verify*.py load the on-disk skill),
so the default path produces ranked candidates + a measurement plan.

Bounds + safety
---------------
  - N (variant count) is clamped to [1, 8]. More variants is more tokens for
    diminishing signal; 3 is the default.
  - Targets STAGING (port 8081) by default. The optimizer only READS the
    verify endpoint (side-effect-free) for the baseline score.
  - NEVER touches the frozen matrix or its manifest. The held-out set is a
    SEPARATE file (evals/<cap>/improvement-set.jsonl). The frozen matrix
    stays the don't-regress guard; the held-out set is what the optimizer is
    allowed to climb.

Token-cost note
---------------
Baseline scoring: (held-out cases) x (samples) verify calls — each a real
LLM compose on staging. With the default 5 samples and a 10-case held-out
set that is 50 compose calls. The variant-proposal step is ONE LLM call that
returns all N variants. If --skill-override-supported is set, measuring the
variants adds N x (cases x samples) more compose calls — budget accordingly
before enabling it.

Usage:
    python scripts/optimize_prompt.py <capability> \
        --skill skills/<name>.md \
        [--n 3] [--samples 5] [--host http://...:8081] \
        [--evals-root ../evals] [--skill-override-supported]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Allow `python scripts/optimize_prompt.py` from anywhere AND
# `from scripts import optimize_prompt` in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import matrix_freeze  # noqa: E402
import run_matrix  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kavi_runtime.private_overlay import read_resolved  # noqa: E402

DEFAULT_STAGING_HOST = run_matrix.DEFAULT_STAGING_HOST  # port 8081
PROD_HOST = run_matrix.PROD_HOST

# Bound on how many variants we ask for. More variants = more tokens for
# diminishing marginal signal, and a wider field the engineer has to deploy
# and measure one at a time. 8 is the ceiling, 3 the sane default.
MAX_VARIANTS = 8
DEFAULT_VARIANTS = 3
DEFAULT_SAMPLES = 5

HTTP_TIMEOUT_SECONDS = run_matrix.HTTP_TIMEOUT_SECONDS

# The three-axis contract (capabilities/CLAUDE.md). A variant skill is
# allowed to change BEHAVIOR only; it must NOT re-encode voice/identity
# (PERSONA axis) or length/format rules (STRUCTURAL axis). We pass this to
# the proposer LLM verbatim so a variant stays inside the contract.
THREE_AXIS_CONTRACT = """\
THREE-AXIS CONTRACT — a skill owns BEHAVIOR ONLY.

  - PERSONA (voice, identity, refusal tone) lives in capabilities/kavi-persona.md
    and is injected at compose time. Do NOT add voice/identity rules to the
    skill. Do NOT restate how Kavi "sounds".
  - STRUCTURAL CONSTRAINTS (length caps, prose-vs-lists, JSON shape) live in
    kavi_runtime/structural_checks.py and are enforced post-compose. Do NOT
    hardcode numeric caps (e.g. "<=120 chars") into the skill; reference the
    behavior, not the literal value.
  - BEHAVIOR (what to surface, when to skip, how to anchor, input-shape rules)
    is the ONLY axis a variant may change.

A variant that adds voice rules or hardcoded length caps is INVALID and will
be rejected by tests/test_composer_skill_isolation.py. Stay inside BEHAVIOR.
"""


# --------------------------------------------------------------------------
# Held-out improvement set: SEPARATE from the frozen matrix.
# --------------------------------------------------------------------------

def improvement_set_path(capability: str, evals_root: Path) -> Path:
    """The held-out improvement set the optimizer is allowed to climb.
    Same row schema as the matrix ({case_id, payload, expect}) but a SEPARATE
    file so editing it never trips the frozen-matrix tamper check."""
    return evals_root / capability / "improvement-set.jsonl"


def load_improvement_rows(capability: str, evals_root: Path) -> list[dict]:
    """Parse the held-out improvement set. Same per-row validation shape as
    the matrix loader (case_id + payload + expect.verdict required), but the
    file lives OUTSIDE matrix/ and is never frozen — it is the climbing
    target, not the regression guard."""
    path = improvement_set_path(capability, evals_root)
    if not path.exists():
        raise FileNotFoundError(
            f"held-out improvement set missing: {path}\n"
            f"Create it (one JSON object per line: "
            f'{{"case_id","payload","expect"}}) — SEPARATE from the frozen '
            f"matrix. See the seed fixture under "
            f"kavi-runtime/tests/fixtures/improvement-set.example.jsonl."
        )
    rows: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: not valid JSON: {e}") from e
        for key in ("case_id", "payload", "expect"):
            if key not in row:
                raise ValueError(f"{path}:{lineno}: row missing required key {key!r}")
        if not isinstance(row["payload"], dict):
            raise ValueError(f"{path}:{lineno}: payload must be a JSON object")
        verdict = (row.get("expect") or {}).get("verdict")
        if verdict not in {"PASS", "FAIL"}:
            raise ValueError(
                f"{path}:{lineno}: expect.verdict must be 'PASS' or 'FAIL', "
                f"got {verdict!r}"
            )
        cid = row["case_id"]
        if cid in seen:
            raise ValueError(f"{path}:{lineno}: duplicate case_id {cid!r}")
        seen.add(cid)
        rows.append(row)
    if not rows:
        raise ValueError(f"{path}: improvement set is empty (no cases)")
    return rows


# --------------------------------------------------------------------------
# Scoring (Step A / Step C) — reuses run_matrix._score_one_case verbatim.
# --------------------------------------------------------------------------

def score_set(
    rows: list[dict],
    url: str,
    samples: int,
    *,
    http_client: httpx.Client | None = None,
    score_fn=None,
) -> dict:
    """Score every held-out row `samples` times via run_matrix._score_one_case
    (reused, not reinvented) and return the aggregate.

    `score_fn` is injectable for tests (default: run_matrix._score_one_case).
    Returns {mean_pass_rate, per_case: {case_id: pass_rate}, per_case_detail}."""
    scorer = score_fn or run_matrix._score_one_case
    owns_client = http_client is None and score_fn is None
    client = http_client
    if client is None and score_fn is None:
        client = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    per_case: list[dict] = []
    try:
        for row in rows:
            sc = scorer(client, url, row, samples)
            per_case.append(sc)
    finally:
        if owns_client and client is not None:
            client.close()
    mean = (
        round(sum(c["pass_rate"] for c in per_case) / len(per_case), 4)
        if per_case else 0.0
    )
    return {
        "mean_pass_rate": mean,
        "per_case": {c["case_id"]: c["pass_rate"] for c in per_case},
        "per_case_detail": per_case,
    }


def weakest_cases(baseline: dict, k: int = 5) -> list[dict]:
    """The held-out cases the current skill is weakest on — the proposer LLM
    gets these as the failures to target."""
    detail = sorted(baseline["per_case_detail"], key=lambda c: c["pass_rate"])
    out = []
    for c in detail[:k]:
        out.append({
            "case_id": c["case_id"],
            "pass_rate": c["pass_rate"],
            "last_details": c.get("last_details") or [],
        })
    return out


# --------------------------------------------------------------------------
# Variant proposal (Step B) — the LLM call is injected.
# --------------------------------------------------------------------------

def build_proposal_prompt(
    skill_text: str, n: int, weak: list[dict], capability: str,
) -> str:
    """The prompt handed to the proposer LLM. Carries the three-axis contract
    + the current skill + the cases it scores worst on, and asks for N variant
    skill texts that stay inside the BEHAVIOR axis."""
    weak_block = "\n".join(
        f"  - {w['case_id']} (current pass-rate {w['pass_rate']:.2f}): "
        f"{'; '.join(w['last_details']) or 'no detail captured'}"
        for w in weak
    ) or "  (no weak cases captured)"
    return (
        f"You are improving the BEHAVIOR-axis skill for the {capability!r} "
        f"LLM composer in HomeOS.\n\n"
        f"{THREE_AXIS_CONTRACT}\n"
        f"CURRENT SKILL TEXT (BEHAVIOR axis):\n"
        f"------------------------------------\n{skill_text}\n"
        f"------------------------------------\n\n"
        f"The held-out cases this skill scores WORST on:\n{weak_block}\n\n"
        f"Propose {n} DISTINCT variant rewrites of the skill that better "
        f"satisfy those held-out cases while staying strictly inside the "
        f"BEHAVIOR axis (no voice rules, no hardcoded length caps). Each "
        f"variant must be a COMPLETE skill text usable as a drop-in "
        f"replacement.\n\n"
        f"Return ONLY a JSON object: "
        f'{{"variants": [{{"label": "...", "rationale": "...", '
        f'"predicted_improvement": <float 0..1>, "skill_text": "..."}}]}}'
    )


def propose_variants(
    proposer_client,
    skill_text: str,
    n: int,
    weak: list[dict],
    capability: str,
) -> list[dict]:
    """Ask the injected proposer client for N variants. The client must
    expose `.propose(prompt: str) -> {"variants": [...]}`. We never make a
    live API call here directly — the client is injected (real one in main(),
    stub in tests). Variants are clamped to n and each is normalized."""
    prompt = build_proposal_prompt(skill_text, n, weak, capability)
    raw = proposer_client.propose(prompt)
    variants = (raw or {}).get("variants") or []
    normalized: list[dict] = []
    for i, v in enumerate(variants[:n]):
        try:
            predicted = float(v.get("predicted_improvement", 0.0))
        except (TypeError, ValueError):
            predicted = 0.0
        normalized.append({
            "label": str(v.get("label") or f"variant-{i + 1}"),
            "rationale": str(v.get("rationale") or ""),
            "predicted_improvement": round(predicted, 4),
            "skill_text": str(v.get("skill_text") or ""),
        })
    return normalized


# --------------------------------------------------------------------------
# Orchestration (Steps A–D) + report.
# --------------------------------------------------------------------------

def optimize(
    capability: str,
    skill_path: Path,
    host: str,
    evals_root: Path,
    *,
    n: int = DEFAULT_VARIANTS,
    samples: int = DEFAULT_SAMPLES,
    proposer_client=None,
    http_client: httpx.Client | None = None,
    score_fn=None,
    skill_override_supported: bool = False,
) -> dict:
    """Run the full optimize flow and write the report. Returns the report
    dict. `proposer_client`, `http_client`, and `score_fn` are injectable for
    tests (no live LLM, no network)."""
    if not (1 <= n <= MAX_VARIANTS):
        raise ValueError(f"--n must be in [1, {MAX_VARIANTS}], got {n}")
    if samples < 1:
        raise ValueError(f"--samples must be >= 1, got {samples}")
    if proposer_client is None:
        raise ValueError("proposer_client is required (inject the LLM client)")
    if not skill_path.exists():
        raise FileNotFoundError(f"skill file missing: {skill_path}")

    skill_text = read_resolved(skill_path)
    skill_name = skill_path.stem  # the skill file name the verify route loads
    rows = load_improvement_rows(capability, evals_root)
    url = f"{host.rstrip('/')}/synthetic/verify/{capability}"
    started_at = datetime.now(timezone.utc)

    # ---- Step A: measured baseline on the held-out set. -------------------
    baseline = score_set(
        rows, url, samples, http_client=http_client, score_fn=score_fn)

    # ---- Step B: propose N variants (injected LLM). -----------------------
    weak = weakest_cases(baseline)
    variants = propose_variants(
        proposer_client, skill_text, n, weak, capability)

    # ---- Step C: write variants; measure ONLY if override path exists. ----
    out_dir = evals_root / capability / "optimizer"
    out_dir.mkdir(parents=True, exist_ok=True)
    variant_records: list[dict] = []
    for i, v in enumerate(variants, start=1):
        variant_file = out_dir / f"variant-{i:02d}.md"
        variant_file.write_text(v["skill_text"])

        rec = {
            "rank": None,  # filled after sort
            "label": v["label"],
            "variant_file": str(variant_file),
            "rationale": v["rationale"],
            "predicted_improvement": v["predicted_improvement"],
            "predicted_mean_pass_rate": round(
                min(1.0, baseline["mean_pass_rate"] + v["predicted_improvement"]),
                4),
            # The keystone honesty flag. Until a variant is deployed to
            # staging and re-scored, its real score is UNKNOWN.
            "measured": False,
            "measured_mean_pass_rate": None,
            "measured_delta": None,
        }

        if skill_override_supported:
            # Only reachable when the verify endpoint accepts a skill_override
            # in the payload. We inject the variant text into each row's
            # payload and re-score for a REAL measured number.
            # The verify route's apply_skill_override expects a
            # {skill_name: text} dict and injects it via the _skill chokepoint
            # (kavi_runtime/runtime/system_prompt.py), so the endpoint composes
            # with the VARIANT instead of the on-disk skill — a real measured
            # score, not a prediction.
            override_rows = [
                {**r, "payload": {**r["payload"],
                                  "skill_override": {skill_name: v["skill_text"]}}}
                for r in rows
            ]
            measured = score_set(
                override_rows, url, samples,
                http_client=http_client, score_fn=score_fn)
            rec["measured"] = True
            rec["measured_mean_pass_rate"] = measured["mean_pass_rate"]
            rec["measured_delta"] = round(
                measured["mean_pass_rate"] - baseline["mean_pass_rate"], 4)
            rec["measured_per_case"] = measured["per_case"]

        variant_records.append(rec)

    # ---- Step D: rank + pick winner + report. -----------------------------
    # Rank by the REAL number when measured; otherwise by the LLM's prediction
    # (clearly flagged as unmeasured). Measured rows always sort ahead of
    # unmeasured ones so a measured win can't be beaten by a mere prediction.
    def _sort_key(r: dict):
        if r["measured"]:
            return (1, r["measured_mean_pass_rate"])
        return (0, r["predicted_mean_pass_rate"])

    variant_records.sort(key=_sort_key, reverse=True)
    for rank, rec in enumerate(variant_records, start=1):
        rec["rank"] = rank

    winner = variant_records[0] if variant_records else None
    any_measured = any(r["measured"] for r in variant_records)

    if winner is None:
        next_step = "No variants proposed — re-run with a richer held-out set."
    elif winner["measured"]:
        next_step = (
            f"Winner {winner['label']!r} is MEASURED at "
            f"{winner['measured_mean_pass_rate']:.3f} "
            f"({winner['measured_delta']:+.3f} vs baseline "
            f"{baseline['mean_pass_rate']:.3f}). Copy "
            f"{winner['variant_file']} over {skill_path}, run the FROZEN "
            f"matrix to confirm no regression, then deploy."
        )
    else:
        next_step = (
            f"UNMEASURED. To measure the recommended winner {winner['label']!r}: "
            f"(1) copy {winner['variant_file']} over {skill_path} on staging "
            f"and redeploy; (2) re-score the held-out set: "
            f"python scripts/optimize_prompt.py {capability} "
            f"--skill {skill_path.name} --samples {samples} "
            f"(or run_matrix on a held-out matrix copy); "
            f"(3) ONLY if the held-out mean climbs AND the FROZEN matrix stays "
            f"green, keep the variant. The predicted improvement "
            f"({winner['predicted_improvement']:+.3f}) is the LLM's claim, NOT "
            f"a measured result."
        )

    report = {
        "ts": started_at.isoformat(),
        "capability": capability,
        "skill_path": str(skill_path),
        "host": host,
        "samples": samples,
        "n_variants": len(variant_records),
        "held_out_set": str(improvement_set_path(capability, evals_root)),
        "held_out_cases": len(rows),
        "baseline": {
            "measured": True,  # the baseline IS the live current skill
            "mean_pass_rate": baseline["mean_pass_rate"],
            "per_case_pass_rate": baseline["per_case"],
            "weakest_cases": weak,
        },
        "variants": variant_records,
        "winner": (
            {
                "label": winner["label"],
                "rank": winner["rank"],
                "measured": winner["measured"],
                "variant_file": winner["variant_file"],
            }
            if winner else None
        ),
        "scores_are_measured": any_measured,
        "honest_limitation": (
            "Variant scores are PREDICTED (the LLM's own claim), not measured, "
            "unless the verify endpoint accepts a skill_override and "
            "--skill-override-supported was set. The baseline is measured "
            "against the live current skill. Deploy a variant to staging and "
            "re-score to turn a prediction into a measured fact."
        ),
        "next_step": next_step,
        "frozen_matrix_untouched": True,
    }

    report_path = out_dir / "optimizer-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    report["report_path"] = str(report_path)

    # ---- console summary --------------------------------------------------
    print(f"[optimize] {capability}: baseline mean pass-rate "
          f"{baseline['mean_pass_rate']:.3f} over {len(rows)} held-out cases "
          f"(samples={samples}).")
    print(f"[optimize] proposed {len(variant_records)} variants "
          f"(measured={any_measured}).")
    for r in variant_records:
        if r["measured"]:
            print(f"  #{r['rank']} {r['label']}: MEASURED "
                  f"{r['measured_mean_pass_rate']:.3f} "
                  f"({r['measured_delta']:+.3f})")
        else:
            print(f"  #{r['rank']} {r['label']}: predicted "
                  f"{r['predicted_mean_pass_rate']:.3f} "
                  f"(+{r['predicted_improvement']:.3f}) — UNMEASURED")
    print(f"[optimize] next step: {next_step}")
    print(f"[optimize] report: {report_path}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capability")
    parser.add_argument("--skill", required=True,
                        help="path to the skill .md file to optimize "
                             "(relative to kavi-runtime/ or absolute)")
    parser.add_argument("--n", type=int, default=DEFAULT_VARIANTS,
                        help=f"variant count (default {DEFAULT_VARIANTS}, "
                             f"max {MAX_VARIANTS})")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help=f"samples per case for the graded score "
                             f"(default {DEFAULT_SAMPLES})")
    parser.add_argument("--host", default=DEFAULT_STAGING_HOST,
                        help=f"verify host (default staging {DEFAULT_STAGING_HOST})")
    parser.add_argument("--prod", action="store_true",
                        help=f"target PRODUCTION ({PROD_HOST}) — burns prod tokens")
    parser.add_argument("--evals-root", default=str(matrix_freeze.DEFAULT_EVALS_ROOT))
    parser.add_argument("--measure", "--skill-override-supported",
                        dest="measure", action="store_true",
                        help="MEASURE each variant for real on staging "
                             "(now supported: the verify route honors a "
                             "skill_override in the payload via the _skill "
                             "chokepoint, 2026-06-24). Costs N x held-out x "
                             "samples staging calls; off by default for cost. "
                             "Without it, the tool ranks by the proposer's "
                             "predicted improvement and flags scores unmeasured.")
    args = parser.parse_args(argv)

    if not (1 <= args.n <= MAX_VARIANTS):
        parser.error(f"--n must be in [1, {MAX_VARIANTS}]")
    if args.samples < 1:
        parser.error("--samples must be >= 1")

    host = PROD_HOST if args.prod else args.host
    skill_path = Path(args.skill)
    if not skill_path.is_absolute():
        skill_path = Path(__file__).resolve().parents[1] / skill_path

    # Real proposer client: the Anthropic-backed composer client. Built lazily
    # so tests never import it. Engineering decision: the proposer reuses the
    # runtime's ClaudeClient so model routing + spend tagging are consistent.
    from kavi_runtime.claude_client import ClaudeClient  # noqa: F401
    from kavi_runtime import config as cfg_mod  # noqa: F401

    class _AnthropicProposer:
        def __init__(self):
            self._client = ClaudeClient(cfg_mod.load_config())

        def propose(self, prompt: str) -> dict:
            return self._client.propose_skill_variants(prompt)

    try:
        report = optimize(
            args.capability,
            skill_path,
            host,
            Path(args.evals_root),
            n=args.n,
            samples=args.samples,
            proposer_client=_AnthropicProposer(),
            skill_override_supported=args.measure,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"[optimize] FAIL: {e}", file=sys.stderr)
        return 1
    return 0 if report.get("winner") else 1


if __name__ == "__main__":
    raise SystemExit(main())
