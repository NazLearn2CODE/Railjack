# Orbiter — Extending and Migrating

This guide explains how to add new LLM providers, services, tools, and roles to Orbiter.

## Adding a Custom LLM Provider

You can add a custom LLM provider in two ways: configuration (for API-compatible endpoints) or code (for new interface implementations).

### 1. Via Environment Configuration (Ambient API)

If your provider is compatible with the Anthropic API (e.g. Ollama, OpenRouter, LocalAI, vLLM), you can register it using the `ORBITER_PROVIDERS` environment variable.

Format:
```bash
export ORBITER_PROVIDERS='[
  {
    "name": "ollama",
    "base_url": "http://localhost:11434/v1",
    "models": ["llama3", "mistral"]
  },
  {
    "name": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "auth_env": "OPENROUTER_API_KEY",
    "models": ["anthropic/claude-3.5-sonnet"]
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
