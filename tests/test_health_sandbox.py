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


def _mcp_servers(main):
    return TestClient(main.app).get("/api/health").json()["mcp_servers"]


def test_health_surfaces_configured_mcp_servers(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_MCP_SERVERS",
        '{"fs": {"type": "stdio", "command": "npx", "args": ["fs-mcp"]}, '
        '"remote": {"type": "sse", "url": "https://example/mcp"}}',
    )
    import app.main as main

    importlib.reload(main)
    by_name = {s["name"]: s for s in _mcp_servers(main)}
    assert by_name["fs"]["type"] == "stdio"
    assert by_name["remote"]["type"] == "sse"


def test_health_reports_no_mcp_servers_when_unset(monkeypatch):
    monkeypatch.delenv("ORBITER_MCP_SERVERS", raising=False)
    import app.main as main

    importlib.reload(main)
    assert _mcp_servers(main) == []


def test_bad_mcp_env_does_not_break_boot(monkeypatch):
    # Malformed JSON logs + empties rather than crashing the gateway at import.
    monkeypatch.setenv("ORBITER_MCP_SERVERS", "{not json")
    import app.main as main

    importlib.reload(main)
    assert _mcp_servers(main) == []


def test_non_dict_mcp_specs_are_dropped(monkeypatch):
    # A non-object spec value must not crash /api/health (spec.get("type")).
    monkeypatch.setenv(
        "ORBITER_MCP_SERVERS",
        '{"fs": {"type": "stdio", "command": "x"}, "bad": "notadict"}',
    )
    import app.main as main

    importlib.reload(main)
    assert {s["name"] for s in _mcp_servers(main)} == {"fs"}
