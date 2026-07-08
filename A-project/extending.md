# Orbiter — Extending and Migrating

This guide explains how to add new LLM providers, services, tools, and roles to Orbiter.

## Adding a Custom LLM Provider

You can add a custom LLM provider in two ways: configuration (for API-compatible endpoints) or code (for new interface implementations).

### Built-in providers

Orbiter ships with three providers in `DEFAULT_PROVIDERS` (`app/core/registry.py`), all routed through one `ClaudeSdkProvider`:

- **z.ai** — ambient default (uses inherited `ANTHROPIC_BASE_URL` + token). No key config beyond the ambient env.
- **anthropic** — native Anthropic, pay-per-token. Put your key in `.env` as `ANTHROPIC_1P_API_KEY=sk-ant-...`.
- **openrouter** — free-tier only (`pricing.prompt == "0"`). Put your key in `.env` as `OPENROUTER_API_KEY=sk-or-v1-...`. Model list fetched from `https://openrouter.ai/api/v1/models`.

Their model lists are fetched live on page load (`POST /api/models/refresh` → `sync_all_models()`), so the dropdown always reflects what each gateway actually offers. Setting `ORBITER_PROVIDERS` (below) **replaces** these defaults entirely.

### 1. Via Environment Configuration (Ambient API)

To add a provider not in the built-in set (e.g. Ollama, LocalAI, vLLM), register it using the `ORBITER_PROVIDERS` environment variable. **Note:** setting this replaces the built-in z.ai/anthropic/openrouter entries — to keep them, include them in your list.

Format:
```bash
export ORBITER_PROVIDERS='[
  {
    "name": "ollama",
    "base_url": "http://localhost:11434/v1",
    "models": ["llama3", "mistral"]
  }
]'
```

- `name` (required): The identifier shown in the model picker dropdown.
- `base_url` (optional): The target API endpoint. If set, overrides `ANTHROPIC_BASE_URL`.
- `auth_env` (optional): The name of the environment variable containing the API key/token. The server reads this variable locally at spawn time and redacts it from the client API.
- `models` (optional): List of model names. If empty, the field is unconstrained.

### 2. Via Code (New Provider Protocol)

If you need to support a completely different SDK contract, implement the `Provider` protocol defined in `app/core/provider.py`:

```python
from typing import Any, AsyncIterator, Optional
from app.core.provider import OnToolUse

class CustomProvider:
    async def stream(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str],
        allowed_tools: list[str],
        session_id: str,
        on_tool_use: OnToolUse,
    ) -> AsyncIterator[dict[str, Any]]:
        # 1. Dispatch your LLM request.
        # 2. Yield event dictionaries conforming to the protocol contract:
        #    - Message: {"type": "message", "role": "user"|"assistant", "content": ..., "uuid": ...}
        #    - Result: {"type": "result", "result": ..., "is_error": ..., "uuid": ...}
        #    - Rate Limit: {"type": "rate_limit", "rate_limit_type": ...}
        pass
```

Register your implementation in `app/main.py` where providers are instantiated.

## Adding a Service

Launcher tiles in the sidebar are configured via the `ORBITER_SERVICES` environment variable.

Format:
```bash
export ORBITER_SERVICES='[
  {
    "name": "Gemini",
    "url": "https://gemini.google.com",
    "embed": true
  },
  {
    "name": "Perplexity",
    "url": "https://www.perplexity.ai",
    "embed": false
  }
]'
```

- `name` (required): The title of the launcher tile.
- `url` (required): The address to launch.
- `embed` (optional): If `true`, the service is loaded in a full-height iframe inside the Console panel. If `false` (or omitted), it opens in a new browser tab.

## Adding a Specialist Agent Role

Specialist roles for Team supervised runs can be composed dynamically in the Composer's `+ ROLES` panel, or configured permanently by editing `DEFAULT_ROLES` in `app/core/orchestrator.py`:

```python
DEFAULT_ROLES = [
    WorkerRole(
        name="researcher",
        system_prompt="You are a meticulous research agent...",
    ),
    WorkerRole(
        name="coder",
        system_prompt="You are an expert software developer...",
        allowed_tools=["Bash", "Write", "Edit"], # Gated dangerous tools
    ),
]
```
