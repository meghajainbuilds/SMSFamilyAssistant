"""One-shot test: send Megha an iMessage via kavi-runtime's BlueBubbles client.

Run from kavi-runtime/ on Kavi's Mac:
    uv run python scripts/test_send.py "your message here"
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.home() / ".config" / "kavi" / ".env")

from kavi_runtime.bluebubbles_client import BlueBubblesClient
from kavi_runtime.main import load_config


def main() -> int:
    msg = sys.argv[1] if len(sys.argv) > 1 else "Kavi v0.2 alive. First iMessage from the always-on runtime."
    config = load_config()
    client = BlueBubblesClient(config)
    temp_guid = f"v02test-{uuid.uuid4().hex[:8]}"
    result = client.send_message(msg, temp_guid)
    print(f"send_result: status={result['status']}")
    print(f"temp_guid: {temp_guid}")
    return 0 if result["status"] in (200, "timeout") else 1


if __name__ == "__main__":
    raise SystemExit(main())
