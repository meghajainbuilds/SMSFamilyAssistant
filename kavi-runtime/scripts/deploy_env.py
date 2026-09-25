"""Private deploy targets for local Python tooling.

Kavi's ssh host and mesh address are not in the public code. They come
from the environment or the gitignored kavi-runtime/.deploy.env (template:
.deploy.env.example), the same source the deploy shell scripts use via
scripts/load_deploy_env.sh. Environment variables win.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEPLOY_ENV_PATH = Path(__file__).resolve().parent.parent / ".deploy.env"
ADDR_UNSET = "KAVI_ADDR_UNSET"


def _file_values() -> dict[str, str]:
    out: dict[str, str] = {}
    if not DEPLOY_ENV_PATH.exists():
        return out
    for line in DEPLOY_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or _file_values().get(name) or default


def require(name: str) -> str:
    value = get(name)
    if not value:
        sys.exit(f"{name} is not set. Export it or add it to {DEPLOY_ENV_PATH} (see .deploy.env.example).")
    return value


def kavi_addr() -> str:
    """Kavi's mesh address, or a loud placeholder when unset (see check_host)."""
    return get("KAVI_ADDR") or ADDR_UNSET


def check_host(host: str) -> None:
    """Exit loudly when a host was built from an unset KAVI_ADDR."""
    if ADDR_UNSET in host:
        require("KAVI_ADDR")
