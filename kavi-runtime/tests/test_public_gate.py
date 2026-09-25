"""Funnel exposes the whole server publicly; only Graph notifications and
/health may answer there (2026-09-25)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from capabilities.realtime_kavi.public_gate import PublicGate, is_private_request


def _app():
    app = FastAPI()
    app.add_middleware(PublicGate)
    for path in ("/health", "/graph/notifications", "/imessage", "/status",
                 "/evals/kavi-persona/recent", "/synthetic/verify/kavi-reply"):
        app.add_api_route(path, lambda: {"ok": True}, methods=["GET", "POST"])
    return app


def test_proxied_public_requests_only_reach_public_paths():
    c = TestClient(_app())
    fwd = {"X-Forwarded-For": "203.0.113.9"}
    assert c.get("/health", headers=fwd).status_code == 200
    assert c.post("/graph/notifications", headers=fwd).status_code == 200
    for path in ("/imessage", "/status", "/evals/kavi-persona/recent", "/synthetic/verify/kavi-reply"):
        assert c.post(path, headers=fwd).status_code == 404, path


def test_local_and_tailnet_callers_reach_everything():
    assert is_private_request("127.0.0.1", {})
    assert is_private_request("100.101.1.2", {})
    assert not is_private_request("127.0.0.1", {"x-forwarded-for": "1.2.3.4"})
    assert not is_private_request("203.0.113.9", {})
    c = TestClient(_app())  # TestClient host "testclient" = local
    assert c.post("/imessage").status_code == 200
    assert c.get("/status").status_code == 200
