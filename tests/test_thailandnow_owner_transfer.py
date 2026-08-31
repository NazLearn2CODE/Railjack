"""Desk-doc ownership transfer (options.transfer_doc_owner).

Every doc created for Paul/Teerin/TIAN is handed to the configured account.
Consumer→consumer Drive transfers are a TWO-STEP consent dance: the owner
grants ``pendingOwner=true`` (Google emails the recipient), then the recipient
accepts with ``role=owner`` + ``transferOwnership=true``. The hub auto-accepts
when a token minted AS the receiving account exists; without it the transfer
stays "pending" on the email. Either way a refusal never loses the doc.
"""

import json

import pytest

from app import thailandnow


class _MockResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or json.dumps(self._json)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _MockAsyncClient:
    """Routes the requests _google_transfer_ownership makes; records everything."""

    calls: list = []  # (method, url, json_body)
    grant_fail = False
    accept_fail = False
    style_fail = False
    recipient_known = True  # recipient already has a permission (inherited from folder)

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, headers=None, params=None):
        type(self).calls.append(("GET", url, {}))
        if url.endswith("/permissions"):
            perms = [{"id": "PERM_PEND", "emailAddress": "thailandnow.info@gmail.com"}] \
                if type(self).recipient_known else []
            return _MockResponse(200, {"permissions": perms})
        if url.endswith("/drive/v3/files"):
            return _MockResponse(200, {"id": "DOC_X"})
        return _MockResponse(200, {"webViewLink": "https://docs.google.com/document/d/DOC_X/edit"})

    async def post(self, url, headers=None, json=None, params=None, content=None):
        type(self).calls.append(("POST", url, json or {}))
        if url.endswith("/drive/v3/files"):
            return _MockResponse(200, {"id": "DOC_X"})
        if url.endswith("/permissions"):
            body = json or {}
            if body.get("pendingOwner"):
                raise AssertionError("pendingOwner must be set via PATCH, create drops it")
            if body.get("type") == "anyone":  # the link-share grant from _google_create_doc
                return _MockResponse(200, {"id": "PERM_ANYONE"})
            assert body.get("emailAddress")
            return _MockResponse(200, {"id": "PERM_NEW"})
        if url.endswith(":batchUpdate"):
            if type(self).style_fail:
                return _MockResponse(500, {}, text="500 styleRefused")
            return _MockResponse(200, {"replies": [{}]})
        raise AssertionError(f"unexpected POST {url}")

    async def patch(self, url, headers=None, json=None, params=None):
        type(self).calls.append(("PATCH", url, json or {}))
        body = json or {}
        if body.get("pendingOwner"):
            assert body["role"] == "writer"
            if type(self).grant_fail:
                return _MockResponse(403, {}, text="403 cannotShare")
            return _MockResponse(200, {"id": "PERM_PEND", "role": "writer"})
        assert body.get("role") == "owner"
        assert (params or {}).get("transferOwnership") == "true"
        assert (headers or {}).get("Authorization") == "Bearer OWNER_TOK"
        if type(self).accept_fail:
            return _MockResponse(400, {}, text="400 badAccept")
        return _MockResponse(200, {"id": "PERM_PEND", "role": "owner"})


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    _MockAsyncClient.calls = []
    _MockAsyncClient.grant_fail = False
    _MockAsyncClient.accept_fail = False
    _MockAsyncClient.style_fail = False
    _MockAsyncClient.recipient_known = True
    monkeypatch.setattr(thailandnow.httpx, "AsyncClient", _MockAsyncClient)


@pytest.mark.anyio
async def test_transfer_autoaccept_ok(monkeypatch):
    async def tok():
        return "OWNER_TOK"
    monkeypatch.setattr(thailandnow, "_google_owner_token", tok)
    link, status = await thailandnow._google_create_doc(
        "tok", "FOLDER", "N", "", transfer_to="thailandnow.info@gmail.com"
    )
    assert link == "https://docs.google.com/document/d/DOC_X/edit"
    assert status == "ok"
    pending = [c for c in _MockAsyncClient.calls
               if c[0] == "PATCH" and c[2].get("pendingOwner")]
    accept = [c for c in _MockAsyncClient.calls
              if c[0] == "PATCH" and c[2].get("role") == "owner"]
    assert len(pending) == 1 and len(accept) == 1
    assert pending[0][2] == {"role": "writer", "pendingOwner": True}
    assert "/permissions/PERM_PEND" in accept[0][1]


@pytest.mark.anyio
async def test_transfer_creates_permission_when_unknown(monkeypatch):
    async def tok():
        return None
    monkeypatch.setattr(thailandnow, "_google_owner_token", tok)
    _MockAsyncClient.recipient_known = False
    link, status = await thailandnow._google_create_doc(
        "tok", "FOLDER", "N", "", transfer_to="x@gmail.com"
    )
    assert link == "https://docs.google.com/document/d/DOC_X/edit"  # doc survives
    assert status.startswith("pending")
    created = [c for c in _MockAsyncClient.calls
               if c[0] == "POST" and c[1].endswith("/permissions")
               and c[2].get("type") == "user"]
    assert len(created) == 1
    assert created[0][2] == {"role": "writer", "type": "user", "emailAddress": "x@gmail.com"}


@pytest.mark.anyio
async def test_transfer_pending_without_owner_token(monkeypatch):
    async def tok():
        return None
    monkeypatch.setattr(thailandnow, "_google_owner_token", tok)
    link, status = await thailandnow._google_create_doc(
        "tok", "FOLDER", "N", "", transfer_to="x@gmail.com"
    )
    assert link == "https://docs.google.com/document/d/DOC_X/edit"  # doc survives
    assert status.startswith("pending")
    assert "x@gmail.com" in status


@pytest.mark.anyio
async def test_transfer_grant_refused_is_soft(monkeypatch):
    _MockAsyncClient.grant_fail = True
    link, status = await thailandnow._google_create_doc(
        "tok", "FOLDER", "N", "", transfer_to="x@gmail.com"
    )
    assert link == "https://docs.google.com/document/d/DOC_X/edit"
    assert status.startswith("failed:")


@pytest.mark.anyio
async def test_transfer_not_requested(monkeypatch):
    async def tok():
        raise AssertionError("owner token must not be fetched when transfer is off")
    monkeypatch.setattr(thailandnow, "_google_owner_token", tok)
    link, status = await thailandnow._google_create_doc("tok", "FOLDER", "N", "")
    assert status == ""
    assert not [c for c in _MockAsyncClient.calls if c[2].get("pendingOwner")]


# --- 1-inch margins (editor spec 2026-08-30) ---


def _batch_update_calls():
    return [c for c in _MockAsyncClient.calls
            if c[0] == "POST" and c[1].endswith(":batchUpdate")]


@pytest.mark.anyio
async def test_margins_pinned_on_every_doc(monkeypatch):
    async def tok():
        return None
    monkeypatch.setattr(thailandnow, "_google_owner_token", tok)
    link, status = await thailandnow._google_create_doc(
        "tok", "FOLDER", "N", "", transfer_to="x@gmail.com"
    )
    assert link  # doc survives regardless
    batches = _batch_update_calls()
    assert len(batches) == 1, "style update runs even with no body text"
    style = batches[0][2]["requests"][0]["updateDocumentStyle"]
    doc_style = style["documentStyle"]
    assert style["fields"] == "marginTop,marginBottom,marginLeft,marginRight"
    for side in ("Top", "Bottom", "Left", "Right"):
        assert doc_style[f"margin{side}"] == {"magnitude": 72, "unit": "PT"}, \
            f"margin{side} must be exactly 1 inch (72 pt)"
    # style must land BEFORE the pendingOwner grant (edit rights still ours)
    pend = [i for i, c in enumerate(_MockAsyncClient.calls)
            if c[0] == "PATCH" and c[2].get("pendingOwner")]
    assert pend and _MockAsyncClient.calls.index(batches[0]) < pend[0]


@pytest.mark.anyio
async def test_style_refused_is_soft(monkeypatch):
    _MockAsyncClient.style_fail = True
    link, status = await thailandnow._google_create_doc("tok", "FOLDER", "N", "")
    assert link == "https://docs.google.com/document/d/DOC_X/edit"  # doc still ships
    assert status == ""
