import importlib
from fastapi.testclient import TestClient
from app.core.provider import ClaudeSdkProvider

def test_api_providers_list(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "models": ["llama3"]}, '
        '{"name": "custom", "auth_env": "CUSTOM_KEY"}]'
    )
    import app.core.registry as registry
    importlib.reload(registry)
    import app.main as main
    importlib.reload(main)

    client = TestClient(main.app)
    r = client.get("/api/providers")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0] == {"name": "ollama", "models": ["llama3"]}
    assert body[1] == {"name": "custom", "models": []}


def test_api_create_session_with_custom_provider(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "models": ["llama3"]}]'
    )
    import app.core.registry as registry
    importlib.reload(registry)
    import app.main as main
    importlib.reload(main)

    client = TestClient(main.app)
    
    # 1. Dispatch with custom provider
    r = client.post("/api/sessions", json={
        "prompt": "hello",
        "provider": "ollama",
        "model": "llama3",
    })
    assert r.status_code == 200
    session_id = r.json()["session_id"]
    
    s = main.manager.get_session(session_id)
    assert isinstance(s.provider, ClaudeSdkProvider)
    assert s.provider._model == "llama3"
    assert s.provider._env == {"ANTHROPIC_BASE_URL": "http://localhost:11434"}

    # 2. Dispatch with invalid provider -> 400
    r = client.post("/api/sessions", json={
        "prompt": "hello",
        "provider": "unknown-provider",
    })
    assert r.status_code == 400
    assert "Unknown provider" in r.json()["detail"]


def test_api_create_team_with_custom_provider(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "models": ["llama3"]}]'
    )
    import app.core.registry as registry
    importlib.reload(registry)
    import app.main as main
    importlib.reload(main)

    client = TestClient(main.app)
    
    # Dispatch team with custom provider
    r = client.post("/api/teams", json={
        "prompt": "hello",
        "provider": "ollama",
        "model": "llama3",
    })
    assert r.status_code == 200
    session_id = r.json()["session_id"]
    
    s = main.manager.get_session(session_id)
    assert isinstance(s.provider, ClaudeSdkProvider)
    assert s.provider._model == "llama3"
    assert s.provider._env == {"ANTHROPIC_BASE_URL": "http://localhost:11434"}
