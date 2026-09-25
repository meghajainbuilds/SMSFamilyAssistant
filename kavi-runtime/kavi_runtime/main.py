"""Entrypoint. Wires the FastAPI server, the scheduler, and graceful shutdown."""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn
import yaml
from dotenv import load_dotenv


CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
DEFAULT_ENV_PATH = Path.home() / ".config" / "kavi" / ".env"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def run() -> None:
    """Console-script entrypoint."""
    load_dotenv(DEFAULT_ENV_PATH)
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit(f"ANTHROPIC_API_KEY missing. Expected at {DEFAULT_ENV_PATH}.")

    configure_logging()
    config = load_config()

    # Phase 3 boot migration (added 2026-06-02). If the legacy monolithic
    # imessage-state.json still exists AND any per-concept file is missing,
    # run the in-process slicer that writes one file per concept and renames
    # the legacy to .pre-phase3-backup. Never raises — boot must succeed even
    # if migration fails (a failed migration leaves the runtime reading from
    # the legacy file, no functional regression). The result dict is logged
    # at INFO so the operator can see the outcome in launchd's stdout log.
    from kavi_runtime.state_per_concept import run_boot_migration
    _state_path = Path(config["paths"]["imessage_state"])
    _migration_result = run_boot_migration(_state_path)
    logging.getLogger(__name__).info(
        "phase3 boot migration: %s", _migration_result
    )

    # Spec-IS-the-runtime drift alarm (added 2026-05-28). Runs the
    # persona loader and security-baseline loader against their
    # canonical spec paths BEFORE the HTTP server accepts webhooks. If
    # either spec is missing, unparseable, or suspiciously empty, the
    # probe raises SystemExit(1) so launchctl logs the exit non-zero
    # and no composer call ever runs against a broken spec. On success,
    # writes a startup marker the /status endpoint surfaces.
    from capabilities.realtime_kavi.startup_probe import verify_spec_loadability
    verify_spec_loadability(config)

    from capabilities.realtime_kavi.scheduler import start_scheduler
    from capabilities.realtime_kavi.server import build_app

    scheduler = start_scheduler(config)
    app = build_app(config, enforce_invariants=True)

    def shutdown(*_args):
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    uvicorn.run(
        app,
        host=config["server"]["host"],
        port=config["server"]["port"],
        log_level="info",
    )


if __name__ == "__main__":
    run()
