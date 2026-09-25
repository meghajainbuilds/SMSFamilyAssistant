"""Unit tests for the deploy + verify scripts (Investment 2).

These tests don't run the scripts against live Kavi (that requires
prod state mutation + bootstrap). They assert the scripts exist, are
executable, and contain the contract pieces the discipline doc names.

Live verification ("verify_deploy.sh exits 0 against current
production state" + induced-failure rollback) is documented in the
commit body that ships these scripts and is run interactively by the
operator.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def test_verify_deploy_script_exists_and_is_executable() -> None:
    p = SCRIPTS_DIR / "verify_deploy.sh"
    assert p.exists(), "scripts/verify_deploy.sh missing"
    assert os.access(p, os.X_OK), "scripts/verify_deploy.sh not executable"


def test_deploy_script_exists_and_is_executable() -> None:
    p = SCRIPTS_DIR / "deploy.sh"
    assert p.exists(), "scripts/deploy.sh missing"
    assert os.access(p, os.X_OK), "scripts/deploy.sh not executable"


def test_verify_deploy_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPTS_DIR / "verify_deploy.sh")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"verify_deploy.sh syntax error: {result.stderr}"


def test_deploy_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPTS_DIR / "deploy.sh")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"deploy.sh syntax error: {result.stderr}"


def test_verify_deploy_script_covers_required_steps() -> None:
    """All four user-visible verification steps named in the discipline
    doc must be in the script. Anyone removing one needs to update the
    doc + this test in the same commit."""
    body = (SCRIPTS_DIR / "verify_deploy.sh").read_text()
    # Step 1: SHA match.
    assert ".deploy_sha" in body, "step 1 (SHA match) missing"
    # Step 2: hash match per dir, all four dirs.
    for d in ("kavi_runtime", "tests", "skills", "capabilities"):
        assert d in body, f"step 2 (hash match) missing dir: {d}"
    assert "shasum -a 256" in body, "step 2 must hash file contents (shasum -a 256)"
    # Step 3: /health.
    assert "/health" in body, "step 3 (/health) missing"
    # Step 4: smoke fixture POST + outbound poll.
    assert "/imessage" in body, "step 4 (smoke fixture POST) missing"
    assert "/evals/kavi-persona/recent" in body, (
        "step 4 outbound assertion missing"
    )


def test_deploy_script_rsyncs_skills_dir() -> None:
    """skills/ was the missed contract item in the 2026-05-08
    post-mortem (miss #4). The deploy script MUST rsync it explicitly."""
    body = (SCRIPTS_DIR / "deploy.sh").read_text()
    assert "skills/" in body, "deploy.sh does not mention skills/ — regression"
    assert "rsync" in body, "deploy.sh does not call rsync"
    assert ".deploy_sha" in body, (
        "deploy.sh must write .deploy_sha after rsync (verify-deploy reads it)"
    )


def test_deploy_script_rsyncs_capabilities_dir() -> None:
    """capabilities/ was added 2026-05-27 as part of the spec-IS-the-
    runtime collapse. The persona spec at capabilities/kavi-persona.md
    is now loaded at composer time via kavi_runtime/persona_loader.py;
    without this rsync block the loader raises FileNotFoundError on Kavi
    and every Kavi-voiced composer call fails."""
    body = (SCRIPTS_DIR / "deploy.sh").read_text()
    assert "capabilities/" in body, (
        "deploy.sh must rsync capabilities/ so persona_loader can read "
        "kavi-persona.md on Kavi"
    )


def test_deploy_script_has_auto_rollback_path() -> None:
    """Discipline rule §8 says 'roll-forward only on green.' deploy.sh
    must auto-rollback to the prior SHA on verify failure."""
    body = (SCRIPTS_DIR / "deploy.sh").read_text()
    assert "rollback" in body.lower(), "deploy.sh missing rollback path"
    assert "git" in body and "worktree" in body, (
        "deploy.sh rollback should use git worktree to materialize prior SHA"
    )


def test_rollback_verifies_against_the_rollback_tree() -> None:
    """2026-09-24: rollback verify compared Kavi to local HEAD, so a good
    rollback always reported 'manual intervention required'."""
    body = (SCRIPTS_DIR / "deploy.sh").read_text()
    rollback = body.split("# ---- Rollback")[1]
    assert 'LOCAL_RUNTIME_DIR="$tmp_worktree/kavi-runtime"' in rollback
    # Worktree must still exist while the rollback verify runs.
    assert rollback.index("verify_deploy.sh") < rollback.index("worktree remove")


def test_verify_hash_excludes_what_deploy_rsync_excludes() -> None:
    """deploy.sh never deletes capabilities/runtime_metrics/ or *.jsonl on
    Kavi, so the hash must skip them too or leftover data fails every deploy."""
    deploy = (SCRIPTS_DIR / "deploy.sh").read_text()
    verify = (SCRIPTS_DIR / "verify_deploy.sh").read_text()
    assert "--exclude='runtime_metrics/' --exclude='*.jsonl'" in deploy
    assert "! -path '*/runtime_metrics/*' ! -name '*.jsonl'" in verify


def _health_verdict(health: str, config_text: str, tmp_path) -> str:
    body = (SCRIPTS_DIR / "verify_deploy.sh").read_text()
    py = body.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config_text)
    out = subprocess.run(["python3", "-c", py], capture_output=True, text=True,
                         env={**os.environ, "HEALTH": health, "CONFIG": str(cfg)})
    return out.stdout.strip()


ACCEPTED = """deploy:
  accepted_health_violations:
    - account: sam@example.com
      until: "2999-01-01"
"""


def test_health_gate_accepts_only_listed_unexpired_accounts(tmp_path) -> None:
    ok = '{"status":"ok"}'
    sam = '{"status":"degraded","violations":[{"account":"sam@example.com"}]}'
    other = '{"status":"degraded","violations":[{"account":"sam@example.com"},{"account":null}]}'
    assert _health_verdict(ok, "", tmp_path) == "ok"
    assert _health_verdict(sam, ACCEPTED, tmp_path) == "ok-accepted"
    assert _health_verdict(other, ACCEPTED, tmp_path).startswith("blocked")
    assert _health_verdict(sam, "", tmp_path).startswith("blocked")
    expired = ACCEPTED.replace("2999-01-01", "2000-01-01")
    assert _health_verdict(sam, expired, tmp_path).startswith("blocked")
