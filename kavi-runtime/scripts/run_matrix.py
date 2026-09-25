#!/usr/bin/env python3
"""run_matrix.py — replay a frozen test matrix against a runtime instance.

Part of the capability-build pipeline (capabilities/BUILD_PIPELINE.md).
POSTs each matrix row's payload to `POST /synthetic/verify/<capability>` on
the target host and asserts the row's expectations:

  row PASSES iff
    returned verdict == expect.verdict
    AND (expect.must_contain_any absent OR any listed substring appears in
         the composed output)
    AND (expect.must_not_contain absent OR none of the listed substrings
         appear in the composed output)

Anti-Goodhart properties enforced here:

  1. FROZEN MATRIX — refuses to run when `matrix_freeze.py check` fails.
     The fix author cannot iterate against an edited matrix.
  2. BOUNDED ITERATION — every invocation increments
     evals/<cap>/matrix/iteration_count.json (reset by `freeze`). Past
     ITERATION_BOUND runs since the last freeze the matrix STILL RUNS (the
     bound is process, not silence) but the runner prints a loud
     "ITERATION BOUND EXCEEDED — stop and surface to PM" banner and exits
     nonzero even when every row passes.

Targets STAGING by default (port 8081 — sandboxed state, outbound hard
disabled). `--prod` targets the production runtime on port 8080 with a
loud banner; production replay still has no side effects (the synthetic
routes are side-effect-free) but burns production-attributed tokens and
shares the event loop with live traffic. Staging green is necessary,
never sufficient: only the production Verifier sub-agent can say "fixed".

Usage:
    python scripts/run_matrix.py <capability> [--host http://...:8081]
                                 [--prod] [--max-failures N]

Exit codes: 0 all rows passed and bound not exceeded; 1 row failures /
matrix-frozen-check refusal / HTTP errors; 3 iteration bound exceeded
(even when green).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Allow `python scripts/run_matrix.py` from anywhere AND `from scripts
# import run_matrix` in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import matrix_freeze  # noqa: E402

import deploy_env  # noqa: E402  (KAVI_ADDR from env or kavi-runtime/.deploy.env)
DEFAULT_STAGING_HOST = f"http://{deploy_env.kavi_addr()}:8081"
PROD_HOST = f"http://{deploy_env.kavi_addr()}:8080"

# Max matrix runs between freezes before the runner forces a stop-and-
# surface. Process bound, not a silent gate: the run still executes so the
# evidence is on the table when the engineer surfaces to the PM.
ITERATION_BOUND = 10

HTTP_TIMEOUT_SECONDS = 180.0  # real LLM composer calls; generous


def _output_as_text(output) -> str:
    """The composed output is a string for text composers (kavi-persona,
    kavi-coordinates) and a dict for inbox-to-task. Normalize for the
    substring checks."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return json.dumps(output, sort_keys=True)


def evaluate_row(row: dict, response_body: dict) -> tuple[bool, list[str]]:
    """Apply the row's expectations to the verify-route response. Returns
    (passed, failure detail strings)."""
    expect = row.get("expect") or {}
    details: list[str] = []

    got_verdict = response_body.get("verdict")
    want_verdict = expect.get("verdict")
    if got_verdict != want_verdict:
        gate_failures = response_body.get("failures") or []
        details.append(
            f"verdict mismatch: expected {want_verdict!r}, got {got_verdict!r}"
            + (f"; gate failures: {gate_failures}" if gate_failures else "")
        )

    output_text = _output_as_text(response_body.get("output"))

    # Substring checks are case-insensitive: a correct reply that capitalizes
    # the required noun at a sentence start ("Teachers meeting…") must still
    # match must_contain_any "teacher". Mirrors run_refusal_tests.py, which
    # has always matched case-insensitively; the kavi-reply matcher had drifted.
    output_lower = output_text.lower()

    must_contain_any = expect.get("must_contain_any") or []
    if must_contain_any and not any(s.lower() in output_lower for s in must_contain_any):
        details.append(
            f"must_contain_any failed: none of {must_contain_any!r} "
            f"appear in output {output_text[:160]!r}"
        )

    must_not_contain = expect.get("must_not_contain") or []
    hits = [s for s in must_not_contain if s.lower() in output_lower]
    if hits:
        details.append(
            f"must_not_contain failed: {hits!r} appear in output "
            f"{output_text[:160]!r}"
        )

    return (not details), details


def _bump_iteration_counter(capability: str, evals_root: Path) -> int:
    """Increment runs_since_freeze and return the new value. Counter file is
    created by `matrix_freeze.py freeze`; if it is missing (legacy matrix
    frozen before this tool existed) we start counting at 1."""
    path = matrix_freeze.iteration_count_path(capability, evals_root)
    counter: dict = {}
    if path.exists():
        try:
            counter = json.loads(path.read_text())
        except json.JSONDecodeError:
            counter = {}
    counter.setdefault("capability", capability)
    counter["runs_since_freeze"] = int(counter.get("runs_since_freeze", 0)) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(counter, indent=2) + "\n")
    return counter["runs_since_freeze"]


def _scorecard_path(capability: str, evals_root: Path) -> Path:
    """Append-only graded scorecard: one row per multi-sample run. Sibling of
    the matrix so the trend lives with the capability's eval surface."""
    return evals_root / capability / "matrix" / "scorecard.jsonl"


def _prior_scorecard_entry(capability: str, evals_root: Path) -> dict | None:
    """The most recent prior scorecard row, for the before/after delta. None
    on the first graded run."""
    path = _scorecard_path(capability, evals_root)
    if not path.exists():
        return None
    last = None
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                last = json.loads(line)
    except (json.JSONDecodeError, OSError):
        return None
    return last


def _score_one_case(
    client: httpx.Client, url: str, row: dict, samples: int,
) -> dict:
    """Run one case `samples` times and return its graded result:
    pass_rate = fraction of samples that pass the row's gates. This is the
    number that climbs — a single sample is a coin flip on borderline rows
    (the bare-yea row flips ~50%); the rate is a stable signal an edit can
    move. Each sample's gate verdict comes from the unchanged evaluate_row."""
    case_id = row.get("case_id", "?")
    n_pass = 0
    last_details: list[str] = []
    last_body: dict = {}
    transport_error: str | None = None
    for _ in range(samples):
        try:
            resp = client.post(url, json=row["payload"])
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:  # noqa: BLE001 — record, keep sampling
            transport_error = f"HTTP/transport error: {e}"
            last_details = [transport_error]
            continue
        last_body = body
        passed, details = evaluate_row(row, body)
        if passed:
            n_pass += 1
        else:
            last_details = details
    pass_rate = (n_pass / samples) if samples else 0.0
    return {
        "case_id": case_id,
        "samples": samples,
        "pass_count": n_pass,
        "pass_rate": round(pass_rate, 4),
        "last_details": last_details,
        "last_verdict": last_body.get("verdict"),
        "last_output": last_body.get("output"),
        "transport_error": transport_error,
    }


def _write_scorecard_and_delta(
    capability: str, evals_root: Path, *, samples: int,
    per_case: list[dict], runs_since_freeze: int, host: str, prod: bool,
    started_at: datetime,
) -> dict:
    """Append the graded scorecard row and print the before/after delta
    against the prior run, so 'this edit moved the score +0.07' is a recorded
    fact, not a vibe. Returns the new scorecard entry."""
    prior = _prior_scorecard_entry(capability, evals_root)
    mean_pass_rate = (
        round(sum(c["pass_rate"] for c in per_case) / len(per_case), 4)
        if per_case else 0.0
    )
    entry = {
        "ts": started_at.isoformat(),
        "capability": capability,
        "host": host,
        "prod": prod,
        "samples": samples,
        "cases": len(per_case),
        "mean_pass_rate": mean_pass_rate,
        "per_case_pass_rate": {c["case_id"]: c["pass_rate"] for c in per_case},
        "runs_since_freeze": runs_since_freeze,
    }
    path = _scorecard_path(capability, evals_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"[run-matrix] GRADED scorecard (samples={samples}): "
          f"mean pass-rate {mean_pass_rate:.3f} over {len(per_case)} cases. "
          f"Log: {path}")
    if prior is None:
        print("[run-matrix] no prior scorecard — this is the baseline run.")
    else:
        overall_delta = mean_pass_rate - prior.get("mean_pass_rate", 0.0)
        sign = "+" if overall_delta >= 0 else ""
        print(f"[run-matrix] DELTA vs prior run ({prior.get('ts', '?')[:19]}): "
              f"mean {sign}{overall_delta:+.3f}")
        prior_rates = prior.get("per_case_pass_rate", {})
        moved = []
        for c in per_case:
            before = prior_rates.get(c["case_id"])
            if before is None:
                continue
            d = c["pass_rate"] - before
            if abs(d) >= 0.01:
                moved.append((c["case_id"], before, c["pass_rate"], d))
        if moved:
            print("[run-matrix] cases that moved:")
            for cid, b, a, d in sorted(moved, key=lambda x: x[3]):
                print(f"        {cid}: {b:.2f} -> {a:.2f} ({d:+.2f})")
        else:
            print("[run-matrix] no case moved >=0.01 since prior run.")
    return entry


def run_matrix(
    capability: str,
    host: str,
    evals_root: Path = matrix_freeze.DEFAULT_EVALS_ROOT,
    *,
    max_failures: int | None = None,
    prod: bool = False,
    samples: int = 1,
    pass_threshold: float = 1.0,
    http_client: httpx.Client | None = None,
) -> int:
    """Run the frozen matrix. Returns the process exit code (see module
    docstring). `http_client` is injectable for tests."""
    # ---- Gate 1: frozen-matrix check. Refuse to run on drift. -------------
    ok, message = matrix_freeze.check(capability, evals_root)
    if not ok:
        print(f"[run-matrix] REFUSING TO RUN — frozen-matrix check failed:\n{message}",
              file=sys.stderr)
        return 1

    rows = matrix_freeze.load_matrix_rows(capability, evals_root)

    # ---- Gate 2: bounded iteration. Counted BEFORE the run so the 11th
    # run is already over the bound; the run still executes (evidence for
    # the PM conversation) but the exit code goes nonzero.
    runs_since_freeze = _bump_iteration_counter(capability, evals_root)
    bound_exceeded = runs_since_freeze > ITERATION_BOUND
    if bound_exceeded:
        print("!" * 72, file=sys.stderr)
        print(
            f"!! ITERATION BOUND EXCEEDED — run {runs_since_freeze} since last "
            f"freeze (bound {ITERATION_BOUND}).\n"
            f"!! Stop and surface to PM (capabilities/BUILD_PIPELINE.md, "
            f"anti-Goodhart rule #2).\n"
            f"!! More iteration without a design conversation is overfitting "
            f"to the matrix.\n"
            f"!! This run still executes for evidence, but the exit code is "
            f"nonzero even if green.",
            file=sys.stderr,
        )
        print("!" * 72, file=sys.stderr)

    if prod:
        print("#" * 72)
        print("# PRODUCTION REPLAY — targeting the LIVE runtime "
              f"({host}).")
        print("# Synthetic routes are side-effect-free, but this burns "
              "production-attributed tokens and shares the event loop with "
              "live traffic. Staging (port 8081) is the default for a reason.")
        print("#" * 72)

    url = f"{host.rstrip('/')}/synthetic/verify/{capability}"
    print(f"[run-matrix] {capability}: {len(rows)} cases -> {url} "
          f"(run {runs_since_freeze} since freeze)")

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)

    graded = samples > 1
    started_at = datetime.now(timezone.utc)
    log_rows: list[dict] = []
    per_case: list[dict] = []
    n_passed = 0
    n_failed = 0
    try:
        for row in rows:
            case_id = row.get("case_id", "?")
            sc = _score_one_case(client, url, row, samples)
            per_case.append(sc)
            # A case is GREEN for the exit-code gate iff its pass-rate clears
            # the threshold. With samples=1 + threshold=1.0 this is identical
            # to the original single-sample pass/fail (rate is 0.0 or 1.0).
            passed = sc["pass_rate"] >= pass_threshold
            if passed:
                n_passed += 1
                rate_note = f" ({sc['pass_count']}/{samples})" if graded else ""
                print(f"  PASS  {case_id}{rate_note}")
            else:
                n_failed += 1
                rate_note = (
                    f" pass-rate {sc['pass_rate']:.2f} ({sc['pass_count']}/{samples})"
                    if graded else ""
                )
                print(f"  FAIL  {case_id}:{rate_note}")
                for d in sc["last_details"]:
                    print(f"        - {d}")
            log_rows.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "case_id": case_id,
                "passed": passed,
                "samples": samples,
                "pass_rate": sc["pass_rate"],
                "details": sc["last_details"],
                "verdict": sc["last_verdict"],
                "output": sc["last_output"],
                "transport_error": sc["transport_error"],
            })
            if max_failures is not None and n_failed >= max_failures:
                print(f"[run-matrix] early stop: {n_failed} failures "
                      f"reached --max-failures {max_failures}")
                break
    finally:
        if owns_client:
            client.close()

    # ---- Run log -----------------------------------------------------------
    runs_dir = evals_root / capability / "matrix" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = started_at.strftime("%Y%m%dT%H%M%SZ")
    log_path = runs_dir / f"run-{ts}.jsonl"
    header = {
        "ts": started_at.isoformat(),
        "capability": capability,
        "host": host,
        "prod": prod,
        "samples": samples,
        "pass_threshold": pass_threshold,
        "runs_since_freeze": runs_since_freeze,
        "iteration_bound": ITERATION_BOUND,
        "bound_exceeded": bound_exceeded,
        "cases_total": len(rows),
        "cases_passed": n_passed,
        "cases_failed": n_failed,
    }
    with log_path.open("w") as f:
        f.write(json.dumps(header) + "\n")
        for r in log_rows:
            f.write(json.dumps(r) + "\n")

    all_green = n_failed == 0 and n_passed == len(rows)
    print(f"[run-matrix] summary: {n_passed} passed, {n_failed} failed, "
          f"{len(rows) - n_passed - n_failed} not run. Log: {log_path}")

    # ---- Graded scorecard + before/after delta (multi-sample runs only).
    # This is the keystone: a number that climbs, recorded per run, so a
    # prompt edit's effect is a measured fact. Single-sample runs keep the
    # original pass/fail-only behavior (no scorecard) for backward compat.
    if graded and per_case:
        _write_scorecard_and_delta(
            capability, evals_root, samples=samples, per_case=per_case,
            runs_since_freeze=runs_since_freeze, host=host, prod=prod,
            started_at=started_at,
        )

    if bound_exceeded:
        print(f"[run-matrix] exit nonzero: iteration bound exceeded "
              f"({runs_since_freeze} > {ITERATION_BOUND}) — stop and surface "
              f"to PM even though the rows "
              f"{'all passed' if all_green else 'did not all pass'}.",
              file=sys.stderr)
        return 3
    return 0 if all_green else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capability")
    parser.add_argument("--host", default=DEFAULT_STAGING_HOST,
                        help=f"target runtime (default staging: {DEFAULT_STAGING_HOST})")
    parser.add_argument("--prod", action="store_true",
                        help=f"target the PRODUCTION runtime ({PROD_HOST}) with a loud banner")
    parser.add_argument("--max-failures", type=int, default=None,
                        help="early-stop after N row failures")
    parser.add_argument("--samples", type=int, default=1,
                        help="run each case N times for a graded pass-rate "
                             "scorecard (default 1 = single-sample pass/fail, "
                             "original behavior). Use 5+ to beat LLM noise and "
                             "see a number that an edit can move.")
    parser.add_argument("--pass-threshold", type=float, default=1.0,
                        help="a case is green for the exit code iff its "
                             "pass-rate >= this (default 1.0 = all samples "
                             "must pass).")
    parser.add_argument("--evals-root", default=str(matrix_freeze.DEFAULT_EVALS_ROOT))
    args = parser.parse_args(argv)

    if args.samples < 1:
        parser.error("--samples must be >= 1")
    if not (0.0 <= args.pass_threshold <= 1.0):
        parser.error("--pass-threshold must be in [0.0, 1.0]")

    host = PROD_HOST if args.prod else args.host
    deploy_env.check_host(host)
    return run_matrix(
        args.capability,
        host,
        Path(args.evals_root),
        max_failures=args.max_failures,
        prod=args.prod,
        samples=args.samples,
        pass_threshold=args.pass_threshold,
    )


if __name__ == "__main__":
    raise SystemExit(main())
