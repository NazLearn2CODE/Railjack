import asyncio
import os
import json
import logging
from typing import Any

logger = logging.getLogger("orbiter")

# Three Anthropic-compatible backends, all routed through the same ClaudeSdkProvider
# seam (resolve() swaps ANTHROPIC_BASE_URL + token per selection; the explicit
# --model flag beats any ambient ANTHROPIC_DEFAULT_*_MODEL alias — verified against
# the bundled CLI's precedence chain). Model lists start empty and are filled live
# by sync_all_models(), so the dropdown never advertises a stale or non-functional
# model. resolve() skips model validation while a list is empty → routing still
# works pre-sync.
#
#   z.ai        — ambient (no base_url; uses inherited ANTHROPIC_BASE_URL + token).
#   anthropic   — native 1P, pay-per-token (ANTHROPIC_1P_API_KEY in .env).
#   openrouter  — free-tier only (pricing.prompt == "0"); OpenRouter's model-list
#                 endpoint lives on the OpenAI surface (/api/v1/models), not the
#                 Anthropic skin — see _fetch_openrouter().
DEFAULT_PROVIDERS = [
    {"name": "z.ai", "models": []},
    {
        "name": "anthropic",
        "base_url": "https://api.anthropic.com",
        "auth_env": "ANTHROPIC_1P_API_KEY",
        "models": [],
    },
    {
        "name": "openrouter",
        "base_url": "https://openrouter.ai/api",
        "auth_env": "OPENROUTER_API_KEY",
        "models": [],
    },
]

# The concrete model the "ambient" (no-explicit-selection) dispatch resolves to.
# z.ai's gateway serves 8 GLM models and silently routes an unspecified --model to
# glm-4.7. Pinning it here means the dashboard's displayed model === the model that
# actually runs — no silent mismatch between the picker and the wire. Only the
# ambient z.ai provider has a known default; a configured provider with no model
# picked stays None (the CLI rejects a bare provider+null rather than guess).
DEFAULT_MODEL = "glm-4.7"


def default_model(name: str) -> str | None:
    """The model an 'ambient'/no-selection dispatch on `name` should run.

    Returns DEFAULT_MODEL for the ambient z.ai provider; None otherwise (a
    configured 2nd provider with no model chosen is left to the caller/CLI to
    reject, never silently guessed).
    """
    return DEFAULT_MODEL if name == "z.ai" else None


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


async def _fetch_anthropic_shape(base: str, token: str) -> list[str]:
    """Fetch a model list from an Anthropic-compatible /v1/models endpoint
    (z.ai gateway, native Anthropic). Response shape: {"data": [{"id": ...}]}.
    """
    import httpx  # declared transitively via claude-agent-sdk; no new dep

    url = base.rstrip("/") + "/v1/models"
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json()
    return sorted({m["id"] for m in data.get("data", []) if m.get("id")})


async def _fetch_openrouter(token: str) -> list[str]:
    """Fetch OpenRouter's FREE-tier models only. OpenRouter's list endpoint lives
    on the OpenAI surface (https://openrouter.ai/api/v1/models), NOT the Anthropic
    skin — using the skin's base would 404. Free = pricing.prompt == "0".
    """
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        data = r.json()
    free = [
        m["id"]
        for m in data.get("data", [])
        if m.get("id") and m.get("pricing", {}).get("prompt") == "0"
    ]
    return sorted(set(free))


async def sync_all_models() -> dict[str, list[str]]:
    """Pull live model lists for every provider in parallel and update each in
    place. Best-effort per provider: a failure (no key, gateway down) logs a
    warning and leaves that provider's list as-is, never blocking the others.
    Returns {provider_name: [models]} for the providers that updated.
    """
    async def _one(provider: dict[str, Any]) -> tuple[str, list[str]] | None:
        name = provider["name"]
        try:
            if name == "z.ai":
                base = os.environ.get("ANTHROPIC_BASE_URL")
                token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
                if not base or not token:
                    logger.warning("sync: z.ai ANTHROPIC_BASE_URL/token not set; skipping")
                    return None
                models = await _fetch_anthropic_shape(base, token)
            elif name == "openrouter":
                token = os.environ.get("OPENROUTER_API_KEY")
                if not token:
                    logger.warning("sync: OPENROUTER_API_KEY not set; skipping openrouter")
                    return None
                models = await _fetch_openrouter(token)
            else:
                base = provider.get("base_url")
                auth_env = provider.get("auth_env")
                token = os.environ.get(auth_env) if auth_env else None
                if not base or not token:
                    logger.warning("sync: %s missing base_url or %s; skipping", name, auth_env or "token")
                    return None
                models = await _fetch_anthropic_shape(base, token)
        except Exception as e:
            logger.warning("sync: %s fetch failed (%s); leaving its list as-is", name, e)
            return None

        provider["models"] = models
        logger.info("sync: %d models from %s", len(models), name)
        return name, models

    results = await asyncio.gather(*(_one(p) for p in PROVIDERS), return_exceptions=True)
    return {r[0]: r[1] for r in results if isinstance(r, tuple)}


async def sync_models() -> list[str]:
    """Backward-compat shim: refresh all providers, return the z.ai model list.
    New callers should use sync_all_models() for the full per-provider picture.
    """
    await sync_all_models()
    provider = next((p for p in PROVIDERS if p["name"] == "z.ai"), None)
    return provider["models"] if provider else []


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
