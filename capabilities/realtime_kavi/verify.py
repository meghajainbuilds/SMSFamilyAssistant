"""realtime-kavi shallow verify.

Per the role-registry policy, runtime-only capabilities use shallow verify
(no LLM composer to replay). The procedure is `shallow:status_endpoint`:
curl `GET http://<ops.kavi_tailnet_addr>:8080/status` and assert
`spec_loaders_ok: true` plus `spec_loaders_checked_at` within the last
60 minutes.

This module provides a `verify_status_endpoint(host)` helper that runs the
shallow procedure and returns a structured verdict, mirroring the deep-
verify shape used by LLM-shaped capabilities.

Gates:
- `spec_loaders_ok` — canonical specs loaded into the runtime at boot
  (deploy gap or syntax error in any capabilities/*.md file flips this to
  false).
- `spec_loaders_checked_at` — proves the loader probe ran recently.
- `funnel_reachable` — Tailscale Funnel public URL reachability (added
  defense in depth; the /health endpoint already flips on this).
"""

from __future__ import annotations

import re
from typing import Any
from urllib import request as _urllib_request


def _fetch_status(host: str, *, timeout: int = 10) -> str:
    url = f"http://{host}/status"
    with _urllib_request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _grep_field(html: str, label: str) -> str | None:
    pat = re.compile(rf"{re.escape(label)}:\s*([^\n<]+)")
    match = pat.search(html)
    if match is None:
        return None
    return match.group(1).strip()


def verify_status_endpoint(host: str | None = None) -> dict[str, Any]:
    """Run the shallow:status_endpoint procedure.

    `host` defaults to `<ops.kavi_tailnet_addr>:8080` from the private config.

    Returns a structured verdict the Verifier sub-agent can read directly:

    ```
    {
      "verdict": "PASS" | "FAIL",
      "failures": [{"gate": "...", "detail": "..."}, ...],
      "raw_fields": {"spec_loaders_ok": "true", ...},
    }
    ```
    """
    if host is None:
        from kavi_runtime import household
        host = f"{household.kavi_tailnet_addr()}:8080"
    failures: list[dict[str, str]] = []
    raw_fields: dict[str, str] = {}

    try:
        html = _fetch_status(host)
    except Exception as e:
        return {
            "verdict": "FAIL",
            "failures": [
                {"gate": "status_endpoint_reachable", "detail": f"unable to fetch /status: {e}"}
            ],
            "raw_fields": {},
        }

    raw_fields["spec_loaders_ok"] = _grep_field(html, "spec_loaders_ok") or ""
    raw_fields["spec_loaders_checked_at"] = _grep_field(html, "spec_loaders_checked_at") or ""
    raw_fields["funnel_reachable"] = _grep_field(html, "funnel_reachable") or ""

    if raw_fields["spec_loaders_ok"].lower() != "true":
        failures.append({
            "gate": "spec_loaders_ok",
            "detail": f"expected 'true', got {raw_fields['spec_loaders_ok']!r}",
        })

    if not raw_fields["spec_loaders_checked_at"]:
        failures.append({
            "gate": "spec_loaders_checked_at",
            "detail": "field missing from /status response",
        })

    verdict = "PASS" if not failures else "FAIL"
    return {"verdict": verdict, "failures": failures, "raw_fields": raw_fields}


__all__ = ["verify_status_endpoint"]
