"""z.ai (GLM) message helper — the one place we call the Anthropic-compatible
z.ai endpoint. Shared by ``comfyui.expand`` and ``notebooklm.polish`` so the
model/headers/key handling isn't duplicated. Raises HTTPException(503) when
``ZAI_API_KEY`` is unset (service unavailable, not a client error)."""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException

ZAI_URL = "https://api.z.ai/api/anthropic/v1/messages"
_MODEL = "glm-5"


async def zai_message(prompt: str, max_tokens: int = 400) -> str:
    """Send ``prompt`` as a single user turn; return the concatenated text.

    Same endpoint/headers/model the old ``comfyui.expand`` inlined. Raises
    HTTPException(503) if the key is unset, or 502 on an upstream error.
    """
    key = os.environ.get("ZAI_API_KEY")
    if not key:
        raise HTTPException(503, "ZAI_API_KEY unset")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(ZAI_URL, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }, json={"model": _MODEL, "max_tokens": max_tokens,
                     "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"z.ai request failed: {e}")
    return "".join(b.get("text", "") for b in data.get("content", [])).strip()
