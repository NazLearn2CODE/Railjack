import os
import json
import logging
from typing import Any

logger = logging.getLogger("orbiter")

DEFAULT_PROVIDERS = [{"name": "default", "models": []}]

class ProviderRegistry:
    def __init__(self):
        self.providers = self._load_providers()

    def _load_providers(self) -> list[dict[str, Any]]:
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
        
        return valid if valid else DEFAULT_PROVIDERS

    def public_view(self) -> list[dict[str, Any]]:
        """Secrets-redacted view: returns only name and models."""
        return [
            {"name": p["name"], "models": p["models"]}
            for p in self.providers
        ]

    def resolve(self, name: str, model: str | None) -> tuple[str | None, dict[str, str] | None]:
        """Resolve a selected provider and model name.

        Returns (resolved_model, env_overrides | None).
        If the name is unknown, raises ValueError.
        """
        provider = next((p for p in self.providers if p["name"] == name), None)
        if not provider:
            raise ValueError(f"Unknown provider: {name}")

        if provider["models"] and model and model not in provider["models"]:
            raise ValueError(f"Unknown model '{model}' for provider '{name}'")

        env_overrides = {}
        if provider.get("base_url"):
            env_overrides["ANTHROPIC_BASE_URL"] = str(provider["base_url"])

        auth_env = provider.get("auth_env")
        if auth_env:
            token = os.environ.get(str(auth_env))
            if token:
                env_overrides["ANTHROPIC_API_KEY"] = token
            else:
                logger.warning("Provider '%s' requires token from env '%s', but it is not set", name, auth_env)

        return model, (env_overrides if env_overrides else None)

REGISTRY = ProviderRegistry()
