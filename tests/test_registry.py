import pytest
from app.core import registry


def test_load_providers_default_when_unset(monkeypatch):
    monkeypatch.delenv("ORBITER_PROVIDERS", raising=False)
    assert registry._load_providers() == registry.DEFAULT_PROVIDERS


def test_load_providers_valid(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "models": ["llama3", "mistral"]}, '
        '{"name": "custom", "auth_env": "CUSTOM_KEY"}]'
    )
    providers = registry._load_providers()
    assert len(providers) == 2
    assert providers[0]["name"] == "ollama"
    assert providers[0]["base_url"] == "http://localhost:11434"
    assert providers[0]["models"] == ["llama3", "mistral"]
    assert providers[1]["name"] == "custom"
    assert providers[1]["auth_env"] == "CUSTOM_KEY"

    # Redaction proof: public_view MUST not serialize base_url or auth_env
    monkeypatch.setattr(registry, "PROVIDERS", providers)
    pv = registry.public_view()
    assert pv == [
        {"name": "ollama", "models": ["llama3", "mistral"]},
        {"name": "custom", "models": []},
    ]


def test_resolve_provider_and_model(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "models": ["llama3"]}, '
        '{"name": "custom", "auth_env": "CUSTOM_KEY"}]'
    )
    monkeypatch.setenv("CUSTOM_KEY", "secret-token")
    monkeypatch.setattr(registry, "PROVIDERS", registry._load_providers())

    # 1. Resolve ollama
    model, env = registry.resolve("ollama", "llama3")
    assert model == "llama3"
    assert env == {"ANTHROPIC_BASE_URL": "http://localhost:11434"}

    # 2. Resolve custom — token goes to ANTHROPIC_AUTH_TOKEN (the var the ambient
    # backend authenticates with), so the SDK env-merge overrides any inherited
    # token rather than leaving a stale one for the CLI to send.
    model, env = registry.resolve("custom", None)
    assert model is None
    assert env == {
        "ANTHROPIC_AUTH_TOKEN": "secret-token",
        "ANTHROPIC_API_KEY": "secret-token",
    }

    # 3. Unknown provider raises ValueError
    with pytest.raises(ValueError, match="Unknown provider"):
        registry.resolve("unknown", None)

    # 4. Unknown model for provider raises ValueError
    with pytest.raises(ValueError, match="Unknown model"):
        registry.resolve("ollama", "invalid-model")


def test_sync_models_fetches_and_updates(monkeypatch):
    """sync_models() pulls the live model list from the ambient gateway and
    updates the z.ai provider in place. Fetcher is stubbed — no real call."""
    import asyncio

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.test/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test")
    monkeypatch.setattr(registry, "PROVIDERS", [{"name": "z.ai", "models": []}])

    async def _fake_anthropic(base, token):
        assert base == "https://gateway.test/api/anthropic"
        return ["glm-4.6", "glm-5.2"]  # fetcher sorts+dedupes

    monkeypatch.setattr(registry, "_fetch_anthropic_shape", _fake_anthropic)

    models = asyncio.run(registry.sync_models())
    assert models == ["glm-4.6", "glm-5.2"]
    assert registry.PROVIDERS[0]["models"] == ["glm-4.6", "glm-5.2"]


def test_sync_models_graceful_failure(monkeypatch):
    """If the gateway is unreachable, sync_models() logs + returns [] without
    raising, and leaves the existing model list untouched."""
    import asyncio

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://down.test/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-test")
    monkeypatch.setattr(registry, "PROVIDERS", [{"name": "z.ai", "models": ["glm-5.2"]}])

    async def _boom(base, token):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(registry, "_fetch_anthropic_shape", _boom)

    models = asyncio.run(registry.sync_models())
    # sync_models() returns the provider's list on failure — which is unchanged
    # from the pre-existing value (the whole point of best-effort sync).
    assert models == ["glm-5.2"]                       # existing list, untouched
    assert registry.PROVIDERS[0]["models"] == ["glm-5.2"]  # preserved on failure


def test_sync_models_skips_when_env_missing(monkeypatch):
    """No base URL or token set → early return, no network attempt."""
    import asyncio

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(registry, "PROVIDERS", [{"name": "z.ai", "models": []}])

    assert asyncio.run(registry.sync_models()) == []


def test_sync_all_models_parallel(monkeypatch):
    """sync_all_models() fetches all providers in parallel; each updates in place."""
    import asyncio

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://zai.test/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zai-tok")
    monkeypatch.setenv("ANTHROPIC_1P_API_KEY", "ant-tok")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-tok")
    monkeypatch.setattr(registry, "PROVIDERS", [
        {"name": "z.ai", "models": []},
        {"name": "anthropic", "base_url": "https://api.anthropic.com",
         "auth_env": "ANTHROPIC_1P_API_KEY", "models": []},
        {"name": "openrouter", "base_url": "https://openrouter.ai/api",
         "auth_env": "OPENROUTER_API_KEY", "models": []},
    ])

    async def _fake_anthropic(base, token):
        return ["glm-5.2"] if "zai" in base or "z.ai" in base else ["claude-sonnet-4"]

    async def _fake_openrouter(token):
        return ["qwen/qwen3-coder:free"]

    monkeypatch.setattr(registry, "_fetch_anthropic_shape", _fake_anthropic)
    monkeypatch.setattr(registry, "_fetch_openrouter", _fake_openrouter)

    result = asyncio.run(registry.sync_all_models())
    assert result == {
        "z.ai": ["glm-5.2"],
        "anthropic": ["claude-sonnet-4"],
        "openrouter": ["qwen/qwen3-coder:free"],
    }
    by_name = {p["name"]: p for p in registry.PROVIDERS}
    assert by_name["z.ai"]["models"] == ["glm-5.2"]
    assert by_name["anthropic"]["models"] == ["claude-sonnet-4"]
    assert by_name["openrouter"]["models"] == ["qwen/qwen3-coder:free"]


def test_sync_all_models_one_failure_doesnt_block_others(monkeypatch):
    """If one gateway fails, the others still populate (return_exceptions=True)."""
    import asyncio

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://zai.test/api/anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "zai-tok")
    monkeypatch.setenv("ANTHROPIC_1P_API_KEY", "ant-tok")
    monkeypatch.setattr(registry, "PROVIDERS", [
        {"name": "z.ai", "models": []},
        {"name": "anthropic", "base_url": "https://api.anthropic.com",
         "auth_env": "ANTHROPIC_1P_API_KEY", "models": []},
    ])

    async def _fake_anthropic(base, token):
        if "anthropic.com" in base:
            raise RuntimeError("anthropic down")
        return ["glm-5.2"]

    monkeypatch.setattr(registry, "_fetch_anthropic_shape", _fake_anthropic)

    result = asyncio.run(registry.sync_all_models())
    assert result == {"z.ai": ["glm-5.2"]}  # anthropic missing → not in result
    by_name = {p["name"]: p for p in registry.PROVIDERS}
    assert by_name["z.ai"]["models"] == ["glm-5.2"]
    assert by_name["anthropic"]["models"] == []  # untouched on failure


def test_fetch_openrouter_free_filter(monkeypatch):
    """_fetch_openrouter keeps only pricing.prompt == '0' (free-tier) models."""
    import asyncio
    import httpx

    # Mix of free + paid; only the two free ones should survive.
    payload = {"data": [
        {"id": "qwen/qwen3-coder:free", "pricing": {"prompt": "0"}},
        {"id": "openai/gpt-5", "pricing": {"prompt": "0.00001"}},  # paid → drop
        {"id": "meta-llama/llama-3.3-70b:free", "pricing": {"prompt": "0"}},
        {"id": "no-pricing-model", "pricing": {}},  # missing → drop
    ]}

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self): return payload

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            assert url == "https://openrouter.ai/api/v1/models"
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    models = asyncio.run(registry._fetch_openrouter("or-tok"))
    assert models == ["meta-llama/llama-3.3-70b:free", "qwen/qwen3-coder:free"]


def test_default_model_zai_ambient():
    """Verify DEFAULT_MODEL constant and default_model() result for z.ai."""
    assert registry.DEFAULT_MODEL == "glm-4.7"
    assert registry.default_model("z.ai") == "glm-4.7"

def test_default_model_other_providers_return_none():
    """Verify default_model() returns None for providers other than z.ai."""
    for name in ("anthropic", "openrouter", "ollama", ""):
        assert registry.default_model(name) is None
