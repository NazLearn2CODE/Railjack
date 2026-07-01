import importlib

from fastapi.testclient import TestClient


def test_health_reports_noop_sandbox_when_disabled(monkeypatch):
    monkeypatch.setenv("ORBITER_SANDBOX", "none")
    import app.main as main

    importlib.reload(main)  # pick up env at startup
    client = TestClient(main.app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "OK"
    sb = body["sandbox"]
    assert sb["mechanism"] == "none"
    assert sb["active"] is False
