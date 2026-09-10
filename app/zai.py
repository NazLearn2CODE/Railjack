"""GLM message helper — the one place we call an Anthropic-compatible endpoint.

Routes through the local **OmniRoute gateway** (`127.0.0.1:20128`, combo
`naz-backup`) instead of z.ai direct: z.ai's TIME_LIMIT quota is the coding-plan
window shared across everything, and it 429s once exhausted. The gateway's combo
cascades glm-5.2 → gemini-2.5-flash → deepseek → free floor, so the call keeps
working after the z.ai rung dies. Shared by ``comfyui.expand``,
``notebooklm.polish`` and ``thailandnow.publicize``. Raises HTTPException(503)
when no key is available (service unavailable, not a client error)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import HTTPException

# OmniRoute gateway (loopback). Combo `naz-backup` = priority failover chain.
GATEWAY_URL = "http://127.0.0.1:20128/v1/messages"
_MODEL = "naz-backup"
# raw z.ai model ids aren't valid gateway model strings — collapse them onto the combo
# so callers still passing the old ids ride the same failover chain.
_ZAI_MODEL_ALIASES = {"glm-5", "glm-5.2"}
# "dsh" = follow whatever model DSH itself is running (agent-default-model in
# ~/.dsh/settings.yaml). DSH provider → gateway provider prefix (z.ai direct lane
# is served by the glm provider-node on the omniroute gateway).
_DSH_SETTINGS = Path.home() / ".dsh" / "settings.yaml"
_DSH_PROVIDER_PREFIX = {"zai": "glm"}


def dsh_default_model() -> str | None:
    """The gateway model id mirroring DSH's current agent-default-model
    (e.g. ``glm/glm-5.3-flash``), or None if unreadable / non-zai provider —
    callers fall back to the naz-backup combo."""
    try:
        import yaml

        cfg = yaml.safe_load(_DSH_SETTINGS.read_text()) or {}
        d = cfg.get("agent-default-model") or {}
        provider, model = d.get("provider"), d.get("model")
        if provider and model:
            prefix = _DSH_PROVIDER_PREFIX.get(provider)
            if prefix:
                return f"{prefix}/{model}"
            return model  # provider already speaks gateway-native ids (e.g. omniroute)
    except Exception:
        pass
    return None


def _resolve_key() -> str | None:
    """OMNIROUTE_API_KEY from env, else read ~/.config/omniroute/.env (the home
    wiring stores it there, not in the shell)."""
    key = os.environ.get("OMNIROUTE_API_KEY")
    if key:
        return key
    env_file = Path.home() / ".config" / "omniroute" / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("OMNIROUTE_API_KEY="):
                val = line.split("=", 1)[1].strip().strip("'\"")
                if val:
                    return val
    return None


async def zai_message(
    prompt: str,
    max_tokens: int = 400,
    system: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Send ``prompt`` (optionally with a ``system`` role, ``model``, ``timeout``)
    as a single user turn; return the concatenated text.

    ``model`` semantics:
    - ``None`` or ``"dsh"`` → follow the model DSH is currently running
      (agent-default-model in ~/.dsh/settings.yaml), prefixed for the gateway.
    - a raw z.ai id (glm-5/glm-5.2) → collapsed onto the `naz-backup` combo.
    - anything else → passed through verbatim (must be gateway-valid).
    Raises 503 if no key, 502 on upstream error.
    """
    key = _resolve_key()
    if not key:
        raise HTTPException(503, "OMNIROUTE_API_KEY unset (and not in ~/.config/omniroute/.env)")
    if model is None or model == "dsh":
        m = dsh_default_model() or _MODEL
    elif model in _ZAI_MODEL_ALIASES:
        m = _MODEL
    else:
        m = model
    payload: dict = {
        "model": m,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(GATEWAY_URL, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"omniroute gateway request failed: {e}")
    return "".join(b.get("text", "") for b in data.get("content", [])).strip()
