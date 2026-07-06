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


def test_api_register_provider_dynamically():
    import app.main as main
    client = TestClient(main.app)
    
    # 1. Post new provider
    r = client.post("/api/providers", json={
        "name": "dynamic-ollama",
        "base_url": "http://localhost:11434",
        "api_key": "my-secret-key",
        "models": ["llama4"],
    })
    assert r.status_code == 200
    
    # 2. Verify in public list
    r = client.get("/api/providers")
    body = r.json()
    names = [p["name"] for p in body]
    assert "dynamic-ollama" in names
    
    # 3. Create session with this provider
    r = client.post("/api/sessions", json={
        "prompt": "hello",
        "provider": "dynamic-ollama",
        "model": "llama4",
    })
    assert r.status_code == 200
    session_id = r.json()["session_id"]
    s = main.manager.get_session(session_id)
    assert s.provider._model == "llama4"
    assert s.provider._env == {
        "ANTHROPIC_BASE_URL": "http://localhost:11434",
        "ANTHROPIC_AUTH_TOKEN": "my-secret-key",
        "ANTHROPIC_API_KEY": "my-secret-key",
    }


def test_resolve_to_sdk_options_env_handoff(monkeypatch):
    """Closes the verification gap between registry.resolve() and the SDK.

    Existing tests assert env overrides land on `provider._env`, but never that
    stream() actually threads them into ClaudeAgentOptions — the handoff point
    where a future refactor could silently drop them. This pins that link, so
    the model-switcher routing is verified at every hop short of a real 2nd key.
    """
    import app.core.provider as provider_mod

    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "auth_env": "OLLAMA_KEY", "models": ["llama3"]}]'
    )
    monkeypatch.setenv("OLLAMA_KEY", "tok-xyz")

    # Reload registry so PROVIDERS reflects the env, then resolve through it —
    # the same path main.create_session() takes.
    import importlib
    import app.core.registry as registry
    importlib.reload(registry)
    model, env_overrides = registry.resolve("ollama", "llama3")
    assert env_overrides == {
        "ANTHROPIC_BASE_URL": "http://localhost:11434",
        "ANTHROPIC_AUTH_TOKEN": "tok-xyz",
        "ANTHROPIC_API_KEY": "tok-xyz",
    }

    # Capture the options the provider would hand the SDK, without spawning the CLI.
    captured: dict = {}

    async def _fake_query(*, prompt, options):
        captured["model"] = options.model
        captured["env"] = options.env
        return
        yield  # unreachable; keeps _fake_query an async generator

    monkeypatch.setattr(provider_mod, "query", _fake_query)

    p = provider_mod.ClaudeSdkProvider(model=model, env=env_overrides)

    async def _run():
        async for _ in p.stream(
            "hi", system_prompt=None, allowed_tools=[],
            session_id="s", on_tool_use=lambda *_: None,
        ):
            pass  # exhaust the generator; the capture happened in _fake_query

    import asyncio
    asyncio.run(_run())

    # The exact env from resolve() must reach ClaudeAgentOptions unchanged.
    assert captured["model"] == "llama3"
    assert captured["env"] == env_overrides


def test_api_fs_list_and_update_workspace(tmp_path):
    import app.main as main
    client = TestClient(main.app)
    
    # 1. List directory
    sub = tmp_path / "subdir"
    sub.mkdir()
    r = client.get(f"/api/fs/list?path={tmp_path}")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == str(tmp_path.resolve())
    assert len(body["dirs"]) == 2
    assert any(d["name"] == "subdir" for d in body["dirs"])
    
    # 2. Update workspace
    r = client.post("/api/workspace-root", json={"root": str(sub)})
    assert r.status_code == 200
    assert r.json()["root"] == str(sub.resolve())
    assert main.WORKSPACE_ROOT == sub.resolve()


def test_delete_session():
    import app.main as main
    client = TestClient(main.app)
    
    # 1. Create a session
    r = client.post("/api/sessions", json={"prompt": "test delete"})
    assert r.status_code == 200
    session_id = r.json()["session_id"]
    assert main.manager.get_session(session_id) is not None
    
    # 2. Delete the session
    r = client.delete(f"/api/sessions/{session_id}")
    assert r.status_code == 200
    assert main.manager.get_session(session_id) is None


def test_resolve_check(tmp_path, monkeypatch):
    import app.main as main
    client = TestClient(main.app)
    monkeypatch.setattr(main, "WORKSPACE_ROOT", tmp_path)
    
    # Resolve claude_md
    r = client.post("/api/health/resolve", json={"check": "claude_md"})
    assert r.status_code == 200
    assert (tmp_path / "CLAUDE.md").is_file()
    
    # Resolve obsidian_mcp
    r = client.post("/api/health/resolve", json={"check": "obsidian_mcp"})
    assert r.status_code == 200
    assert (tmp_path / ".mcp.json").is_file()


