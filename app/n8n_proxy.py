"""Reverse proxy for the n8n editor under ``/n8n/``.

Phase E: the hub is browsed at ``localhost:8700`` but the iframe pointed at
``localhost:5678`` — a *different origin* (port differs), so n8n's own
``X-Frame-Options: SAMEORIGIN`` header refused to be framed AND its auth cookie
counted as third-party. Same-origin proxying fixes both at once: the iframe src
is ``/n8n/`` (same origin as the hub), n8n's cookie is first-party, and
``SAMEORIGIN`` is satisfied.

n8n is configured (``~/n8n/n8n.env``) with ``N8N_PATH=/n8n/`` so its editor HTML
references assets as ``/n8n/assets/...``. n8n itself serves those assets at root
(``/assets/...``), so the proxy STRIPS the ``/n8n`` prefix when forwarding —
``/n8n/assets/x`` → ``127.0.0.1:5678/assets/x``. Root paths (``/healthz``,
``/api/v1``, ``/webhook``) are untouched on the direct port, so server-side
curls and ``api.sh`` still work.

Loopback upstream only (``127.0.0.1:5678``) — home firewall posture unverified.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.datastructures import Headers

router = APIRouter()

N8N_UPSTREAM = "http://127.0.0.1:5678"

# RFC 7230 hop-by-hop headers (never forwarded) + framing headers the response
# layer re-derives. content-encoding is dropped because we stream decoded bytes
# via aiter_bytes(); content-length/transfer-encoding are set by StreamingResponse.
_DROP_REQ = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "content-length",
    # accept-encoding dropped here, then re-set to "identity" below — httpx's
    # AsyncClient adds its own "gzip, deflate" by default, so merely dropping
    # the inbound value isn't enough.
    "accept-encoding",
}
_DROP_RESP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "content-encoding",
}


@router.api_route(
    "/n8n/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_n8n(path: str, request: Request) -> StreamingResponse:
    # Strip the /n8n prefix: n8n serves its editor + assets at root; N8N_PATH
    # only rewrites the HTML's asset URLs to /n8n/... so they route back here.
    target = f"{N8N_UPSTREAM}/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
    # Force identity upstream: n8n serves an uncompressed body, so we stream
    # bytes straight through (loopback, bandwidth is free) and the browser
    # renders HTML instead of a raw gzip stream.
    fwd["accept-encoding"] = "identity"

    # ponytail: per-request client (one-user hub, low traffic). A shared
    # connection pool with app-level lifecycle is the upgrade path if this ever
    # shows up in profiles.
    client = httpx.AsyncClient(timeout=None, follow_redirects=False)
    upstream = await client.send(
        client.build_request(
            request.method, target, headers=fwd, content=await request.body()
        ),
        stream=True,
    )

    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    # Headers(raw=...) so duplicate upstream headers — e.g. multiple Set-Cookie
    # on auth — survive StreamingResponse.init_headers. multi_items() keeps
    # repeats that .items()/a dict would collapse.
    resp_headers = Headers(
        raw=[
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in upstream.headers.multi_items()
            if k.lower() not in _DROP_RESP
        ]
    )
    return StreamingResponse(
        relay(), status_code=upstream.status_code, headers=resp_headers
    )
