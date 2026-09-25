"""Public-internet gate for the runtime (2026-09-25, public-repo step 6).

Tailscale Funnel forwards the whole server to a public hostname. Only two
routes need to be public: Microsoft Graph change notifications and the
liveness ping. Everything else (evals/recent pages with email subjects and
message text, /synthetic routes that spend LLM budget, the iMessage webhook
that Kavi would act on) must answer only on the machine itself or the
private tailnet.

How a request is classified:
- Funnel/serve proxy requests reach the app from loopback WITH an
  X-Forwarded-For header (the proxy adds it). Those are public.
- Loopback with no forwarding header = a local caller (BlueBubbles, the
  deploy check over SSH, the scheduler). Private.
- A direct tailnet client address (100.64.0.0/10, e.g. run_matrix from
  Megha's Mac to :8081) = private.
- Anything else = public.

Public requests to any path outside PUBLIC_PATHS get a plain 404, so the
public hostname reveals nothing about what else the server has.
"""

from __future__ import annotations

import ipaddress
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/graph/notifications", "/health"})
_TAILNET = ipaddress.ip_network("100.64.0.0/10")
_FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "tailscale-funnel-request")


def is_private_request(client_host: str | None, headers) -> bool:
    if any(h in headers for h in _FORWARD_HEADERS):
        return False
    if not client_host:
        return False
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return client_host in ("testclient", "localhost")
    return ip.is_loopback or ip in _TAILNET


class PublicGate(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if path in PUBLIC_PATHS:
            return await call_next(request)
        client = request.client.host if request.client else None
        if is_private_request(client, request.headers):
            return await call_next(request)
        logger.warning("public_gate: blocked public %s %s", request.method, path[:80])
        return PlainTextResponse("Not Found", status_code=404)
