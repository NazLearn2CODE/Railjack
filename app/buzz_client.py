"""Async HTTP client for a Buzz relay — Python port of buzz-cli's client.rs.

Every write/read goes through the relay's REST bridge (``POST /events``,
``POST /query``), authenticated per-request with a NIP-98 (kind 27235) event
signed over the request URL+method+body. This is a deliberately thin
reimplementation of exactly what ``buzz-cli`` (Rust) does — see
crates/buzz-cli/src/client.rs and crates/buzz-sdk/src/builders.rs in the
Buzz repo for the reference. No websocket, no retry/backoff — Railjack's
poll cadence makes that unnecessary for a chat panel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import httpx

from .nostr_event import build_event

KIND_MESSAGE = 9
KIND_CREATE_CHANNEL = 9007
KIND_JOIN = 9021
KIND_NIP98_AUTH = 27235


class BuzzError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"buzz relay error {status}: {body}")


def _nip98_header(privkey_hex: str, method: str, url: str, body: bytes | None) -> str:
    tags = [["u", url], ["method", method], ["nonce", str(uuid.uuid4())]]
    if body:
        tags.append(["payload", hashlib.sha256(body).hexdigest()])
    event = build_event(privkey_hex, KIND_NIP98_AUTH, "", tags)
    encoded = base64.b64encode(json.dumps(event).encode("utf-8")).decode("ascii")
    return f"Nostr {encoded}"


class BuzzClient:
    def __init__(self, relay_url: str, privkey_hex: str, timeout: float = 15.0):
        self.relay_url = relay_url.rstrip("/")
        self.privkey_hex = privkey_hex
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self):
        await self._http.aclose()

    async def _post_authed(self, path: str, body_obj) -> str:
        url = f"{self.relay_url}{path}"
        body = json.dumps(body_obj).encode("utf-8")
        auth = _nip98_header(self.privkey_hex, "POST", url, body)
        resp = await self._http.post(
            url,
            content=body,
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        return self._unwrap(resp)

    def _unwrap(self, resp: httpx.Response) -> str:
        if resp.status_code >= 300:
            try:
                data = resp.json()
                msg = data.get("error") or data.get("message") or resp.text
            except Exception:
                msg = resp.text
            raise BuzzError(resp.status_code, msg)
        return resp.text

    async def submit_event(self, event: dict) -> dict:
        raw = await self._post_authed("/events", event)
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}

    async def query(self, filter_obj: dict) -> list[dict]:
        raw = await self._post_authed("/query", [filter_obj])
        return json.loads(raw)

    # ---- helpers mirroring buzz-cli's channels/messages subcommands ----

    async def create_channel(
        self, name: str, channel_type: str = "stream", visibility: str = "open",
        about: str | None = None,
    ) -> str:
        channel_id = str(uuid.uuid4())
        tags = [["h", channel_id], ["name", name], ["visibility", visibility],
                ["channel_type", channel_type]]
        if about:
            tags.append(["about", about])
        event = build_event(self.privkey_hex, KIND_CREATE_CHANNEL, "", tags)
        await self.submit_event(event)
        return channel_id

    async def join_channel(self, channel_id: str) -> None:
        event = build_event(self.privkey_hex, KIND_JOIN, "", [["h", channel_id]])
        await self.submit_event(event)

    async def send_message(self, channel_id: str, content: str) -> dict:
        event = build_event(self.privkey_hex, KIND_MESSAGE, content, [["h", channel_id]])
        return await self.submit_event(event)

    async def get_messages(self, channel_id: str, limit: int = 50, since: int | None = None) -> list[dict]:
        filt = {"kinds": [KIND_MESSAGE], "#h": [channel_id], "limit": min(limit, 200)}
        if since is not None:
            filt["since"] = since
        events = await self.query(filt)
        events.sort(key=lambda e: e.get("created_at", 0))
        return events
