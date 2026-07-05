import pytest
from app.core.registry import ProviderRegistry

def test_load_providers_default_when_unset(monkeypatch):
    monkeypatch.delenv("ORBITER_PROVIDERS", raising=False)
    reg = ProviderRegistry()
    assert reg.providers == [{"name": "default", "models": []}]
    assert reg.public_view() == [{"name": "default", "models": []}]


def test_load_providers_valid(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "models": ["llama3", "mistral"]}, '
        '{"name": "custom", "auth_env": "CUSTOM_KEY"}]'
    )
    reg = ProviderRegistry()
    assert len(reg.providers) == 2
    assert reg.providers[0]["name"] == "ollama"
    assert reg.providers[0]["base_url"] == "http://localhost:11434"
    assert reg.providers[0]["models"] == ["llama3", "mistral"]
    assert reg.providers[1]["name"] == "custom"
    assert reg.providers[1]["auth_env"] == "CUSTOM_KEY"

    # Redaction proof: public_view MUST not serialize base_url or auth_env
    pv = reg.public_view()
    assert len(pv) == 2
    assert pv[0] == {"name": "ollama", "models": ["llama3", "mistral"]}
    assert pv[1] == {"name": "custom", "models": []}


def test_resolve_provider_and_model(monkeypatch):
    monkeypatch.setenv(
        "ORBITER_PROVIDERS",
        '[{"name": "ollama", "base_url": "http://localhost:11434", "models": ["llama3"]}, '
        '{"name": "custom", "auth_env": "CUSTOM_KEY"}]'
    )
    monkeypatch.setenv("CUSTOM_KEY", "secret-token")
    reg = ProviderRegistry()
    
    # 1. Resolve ollama
    model, env = reg.resolve("ollama", "llama3")
    assert model == "llama3"
    assert env == {"ANTHROPIC_BASE_URL": "http://localhost:11434"}

    # 2. Resolve custom
    model, env = reg.resolve("custom", None)
    assert model is None
    assert env == {"ANTHROPIC_API_KEY": "secret-token"}

    # 3. Unknown provider raises ValueError
    with pytest.raises(ValueError, match="Unknown provider"):
        reg.resolve("unknown", None)

    # 4. Unknown model for provider raises ValueError
    with pytest.raises(ValueError, match="Unknown model"):
        reg.resolve("ollama", "invalid-model")
