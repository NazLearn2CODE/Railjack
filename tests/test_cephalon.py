import json
import importlib
from fastapi.testclient import TestClient
from app.core.cephalon import probe

def test_cephalon_probe(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    # 1. Level: none
    probe.cache_clear()
    res = probe(workspace)
    assert res["level"] == "none"
    assert res["checks"] == {
        "claude_md": False,
        "code_compass": False,
        "project_index": False,
        "obsidian_mcp": False,
    }

    # 2. Level: partial
    (workspace / "CLAUDE.md").write_text("Hello", encoding="utf-8")
    probe.cache_clear()
    res = probe(workspace)
    assert res["level"] == "partial"
    assert res["checks"]["claude_md"] is True
    assert res["checks"]["code_compass"] is False

    # 3. Level: full
    (workspace / "CodeCompass.md").write_text("Hello", encoding="utf-8")
    (workspace / "A-project").mkdir()
    (workspace / "A-project" / "index.md").write_text("Hello", encoding="utf-8")
    (workspace / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "obsidian": {"type": "stdio"}
        }
    }), encoding="utf-8")
    
    probe.cache_clear()
    res = probe(workspace)
    assert res["level"] == "full"
    assert res["checks"]["claude_md"] is True
    assert res["checks"]["code_compass"] is True
    assert res["checks"]["project_index"] is True
    assert res["checks"]["obsidian_mcp"] is True


def test_health_endpoint_surfaces_workspace_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBITER_WORKSPACE_ROOT", str(tmp_path))
    import app.main as main
    importlib.reload(main)

    client = TestClient(main.app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "workspace" in body
    assert body["workspace"]["root"] == str(tmp_path.resolve())
    assert body["workspace"]["level"] == "none"
