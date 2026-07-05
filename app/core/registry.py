import os
import json
import logging
from typing import Any

logger = logging.getLogger("orbiter")

DEFAULT_PROVIDERS = [{"name": "default", "models": []}]


def _load_providers() -> list[dict[str, Any]]:
    raw = os.environ.get("ORBITER_PROVIDERS", "").strip()
    if not raw:
        return DEFAULT_PROVIDERS
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("ORBITER_PROVIDERS is not valid JSON (%s); using default provider", e)
        return DEFAULT_PROVIDERS
    if not isinstance(parsed, list):
        logger.error("ORBITER_PROVIDERS must be a JSON list; using default provider")
        return DEFAULT_PROVIDERS

    valid = []
    for item in parsed:
        if isinstance(item, dict) and "name" in item:
            valid.append({
                "name": str(item["name"]),
                "base_url": item.get("base_url"),
                "auth_env": item.get("auth_env"),
                "models": list(item.get("models") or []),
            })
        else:
            logger.error("ORBITER_PROVIDERS: skipped invalid provider spec: %s", item)

    return valid or DEFAULT_PROVIDERS


# Loaded once at import (env is set before the gateway starts). Single config for a
# local single-user OS — no per-instance state, so plain module funcs, not a class.
PROVIDERS = _load_providers()


def add_provider(
    name: str,
    base_url: str | None = None,
    auth_env: str | None = None,
    api_key: str | None = None,
    models: list[str] | None = None,
):
    """Add or update a provider in the registry dynamically."""
    existing = next((p for p in PROVIDERS if p["name"] == name), None)
    spec = {
        "name": name,
        "base_url": base_url,
        "auth_env": auth_env,
        "api_key": api_key,
        "models": models or [],
    }
    if existing:
        existing.update({k: v for k, v in spec.items() if v is not None})
    else:
        PROVIDERS.append(spec)


def public_view() -> list[dict[str, Any]]:
    """Secrets-redacted view: name + models only (base_url/auth_env never leave)."""
    return [{"name": p["name"], "models": p["models"]} for p in PROVIDERS]


def resolve(name: str, model: str | None) -> tuple[str | None, dict[str, str] | None]:
    """Resolve a selected provider+model to (model, env_overrides | None).

    Raises ValueError on an unknown provider name or a model not in its list.
    """
    provider = next((p for p in PROVIDERS if p["name"] == name), None)
    if not provider:
        raise ValueError(f"Unknown provider: {name}")

    if provider["models"] and model and model not in provider["models"]:
        raise ValueError(f"Unknown model '{model}' for provider '{name}'")

    env_overrides: dict[str, str] = {}
    if provider.get("base_url"):
        env_overrides["ANTHROPIC_BASE_URL"] = str(provider["base_url"])

    auth_env = provider.get("auth_env")
    if auth_env:
        token = os.environ.get(str(auth_env))
        if token:
            # Write the SAME var the ambient backend authenticates with (z.ai/GLM
            # uses ANTHROPIC_AUTH_TOKEN → Bearer). The SDK merges options.env over
            # the inherited env, so this overrides any inherited token — the
            # subprocess sends THIS provider's credential to base_url, never a stale
            # one. Using a different var (e.g. ANTHROPIC_API_KEY) would leave the
            # inherited AUTH_TOKEN in place and the CLI would send the wrong one.
            env_overrides["ANTHROPIC_AUTH_TOKEN"] = token
            env_overrides["ANTHROPIC_API_KEY"] = token
        else:
            logger.warning("Provider '%s' requires token from env '%s', but it is not set", name, auth_env)

    api_key = provider.get("api_key")
    if api_key:
        env_overrides["ANTHROPIC_AUTH_TOKEN"] = str(api_key)
        env_overrides["ANTHROPIC_API_KEY"] = str(api_key)

    return model, (env_overrides or None)
