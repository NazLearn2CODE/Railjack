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
