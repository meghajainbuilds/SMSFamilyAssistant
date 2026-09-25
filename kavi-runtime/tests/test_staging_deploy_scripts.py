"""Unit tests for the staging deploy artifacts (capability-build pipeline).

Mirrors tests/test_deploy_scripts.py: no live Kavi interaction — asserts
the scripts exist, are executable, pass `bash -n`, and contain the
contract pieces; the plist parses and carries the staging service shape;
config-staging.yaml parses with the staging invariants (staging_mode on,
port 8081, no healthcheck ping, no public URL, sandboxed paths).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import yaml

RUNTIME_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = RUNTIME_DIR / "scripts"

DEPLOY = SCRIPTS_DIR / "deploy_staging.sh"
INSTALL = SCRIPTS_DIR / "install_staging_service.sh"
PLIST = SCRIPTS_DIR / "com.megha.kavi-staging.plist"
def _cfg(name: str) -> Path:
    """Real config when present (gitignored, private); the committed
    fictional-household example otherwise (fresh public clone)."""
    real = RUNTIME_DIR / name
    return real if real.exists() else RUNTIME_DIR / name.replace(".yaml", ".example.yaml")


CONFIG = _cfg("config-staging.yaml")


# ---- deploy_staging.sh --------------------------------------------------------

def test_deploy_staging_exists_and_is_executable() -> None:
    assert DEPLOY.exists(), "scripts/deploy_staging.sh missing"
    assert os.access(DEPLOY, os.X_OK), "scripts/deploy_staging.sh not executable"


def test_deploy_staging_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(DEPLOY)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"deploy_staging.sh syntax error: {result.stderr}"


def test_deploy_staging_covers_required_steps() -> None:
    text = DEPLOY.read_text()
    # Ships config-staging.yaml AS the instance's config.yaml.
    assert "config-staging.yaml" in text
    assert "$KAVI_STAGING_DIR/config.yaml" in text
    # Targets the staging tree + service, never production's.
    assert "/Users/kavi/kavi-staging" in text
    assert "com.megha.kavi-staging" in text
    assert "/Users/kavi/kavi-runtime" not in text.replace(
        "# Production deploys keep", "")  # no prod dir reference in commands
    # Writes the deployed SHA and kickstarts.
    assert ".deploy_sha" in text
    assert "launchctl kickstart" in text
    # Verifies health on the staging port; disposable (no rollback).
    assert "8081/health" in text
    assert "rollback" not in text.lower().replace("no auto-rollback", "").replace(
        "rollback contract", "")


def test_deploy_staging_rsyncs_same_file_sets_as_production() -> None:
    """deploy.sh contract: kavi_runtime/ + tests/ + skills/ + capabilities/
    + scripts/ + config. Staging mirrors it (plus pyproject for the
    editable venv install)."""
    text = DEPLOY.read_text()
    for piece in ("kavi_runtime/", "tests/", "skills/", "capabilities/",
                  "scripts/", "pyproject.toml"):
        assert piece in text, f"deploy_staging.sh does not ship {piece}"


def test_deploy_staging_creates_sandboxed_state_tree() -> None:
    text = DEPLOY.read_text()
    assert "HomeOS-staging" in text
    assert "mkdir -p" in text


# ---- install_staging_service.sh ------------------------------------------------

def test_install_script_exists_and_is_executable() -> None:
    assert INSTALL.exists(), "scripts/install_staging_service.sh missing"
    assert os.access(INSTALL, os.X_OK)


def test_install_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(INSTALL)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"install_staging_service.sh syntax error: {result.stderr}"


def test_install_script_bootstraps_launchd() -> None:
    text = INSTALL.read_text()
    assert "LaunchAgents" in text
    assert "launchctl bootstrap" in text
    assert "launchctl kickstart" in text
    # Documents the one-time venv creation contract.
    assert "python3 -m venv .venv" in text
    assert "pip install -e ." in text


# ---- launchd plist -------------------------------------------------------------

def test_staging_plist_parses_with_expected_shape() -> None:
    assert PLIST.exists(), "scripts/com.megha.kavi-staging.plist missing"
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "com.megha.kavi-staging"
    assert data["WorkingDirectory"] == "/Users/kavi/kavi-staging"
    assert data["ProgramArguments"] == ["/Users/kavi/kavi-staging/.venv/bin/kavi-runtime"]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert "kavi-staging.out.log" in data["StandardOutPath"]
    assert "kavi-staging.err.log" in data["StandardErrorPath"]


# ---- config-staging.yaml -------------------------------------------------------

def test_config_staging_parses_with_staging_invariants() -> None:
    assert CONFIG.exists(), "config-staging.yaml missing"
    config = yaml.safe_load(CONFIG.read_text())
    assert config["staging_mode"] is True
    assert config["server"]["port"] == 8081
    assert config["server"]["public_url"] is None  # no Funnel for staging
    assert config["healthcheck"]["url"] is None  # never ping prod's check
    assert config["action_layer"]["dry_run"] is True


def test_config_staging_paths_are_sandboxed() -> None:
    """No state/eval path may point at the production HomeOS tree —
    synthetic-replay rows must not pollute production eval surfaces.
    Spec/skill paths live in the staging deploy dir (kavi-staging)."""
    config = yaml.safe_load(CONFIG.read_text())
    for key, value in config["paths"].items():
        assert "/Users/kavi/HomeOS/" not in str(value), (
            f"paths.{key} points into the production HomeOS tree: {value}"
        )
        assert "/Users/kavi/kavi-runtime/" not in str(value), (
            f"paths.{key} points into the production runtime dir: {value}"
        )


def test_config_staging_has_same_top_level_keys_as_production() -> None:
    """Structure copied from config.yaml — config readers must not
    KeyError on the staging instance. staging_mode is the one addition."""
    prod = yaml.safe_load(_cfg("config.yaml").read_text())
    staging = yaml.safe_load(CONFIG.read_text())
    missing = set(prod) - set(staging)
    assert not missing, f"config-staging.yaml missing top-level keys: {missing}"
    assert set(staging) - set(prod) == {"staging_mode"}
