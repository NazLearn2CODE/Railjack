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
    """Routes the calls _google_create_doc makes and records every request."""

    calls: list = []  # (method, url, json_body)
    grant_fail = False
    accept_fail = False

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, headers=None, json=None, params=None, content=None):
        type(self).calls.append(("POST", url, json or {}))
        if url.endswith("/drive/v3/files"):
            return _MockResponse(200, {"id": "DOC_X"})
        if url.endswith("/permissions"):
            body = json or {}
            if body.get("pendingOwner"):
                # the consent dance: writer + pendingOwner, NOT a direct owner grant
                assert body["role"] == "writer" and body.get("emailAddress")
                if type(self).grant_fail:
                    return _MockResponse(403, {}, text="403 cannotShare")
                return _MockResponse(200, {"id": "PERM_PEND", "role": "writer"})
            return _MockResponse(200, {"id": "PERM_ANYONE"})
        raise AssertionError(f"unexpected POST {url}")

    async def patch(self, url, headers=None, json=None, params=None):
        type(self).calls.append(("PATCH", url, json or {}))
        body = json or {}
        assert body.get("role") == "owner"
        assert (params or {}).get("transferOwnership") == "true"
        assert (headers or {}).get("Authorization") == "Bearer OWNER_TOK"
        if type(self).accept_fail:
            return _MockResponse(400, {}, text="400 badAccept")
        return _MockResponse(200, {"id": "PERM_PEND", "role": "owner"})

    async def get(self, url, headers=None, params=None):
        type(self).calls.append(("GET", url, {}))
        return _MockResponse(200, {"webViewLink": "https://docs.google.com/document/d/DOC_X/edit"})


@pytest.fixture(autouse=True)
def _patch_http(monkeypatch):
    _MockAsyncClient.calls = []
    _MockAsyncClient.grant_fail = False
    _MockAsyncClient.accept_fail = False
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
    grant = [c for c in _MockAsyncClient.calls if c[1].endswith("/permissions") and c[2].get("pendingOwner")]
    accept = [c for c in _MockAsyncClient.calls if c[0] == "PATCH"]
    assert len(grant) == 1 and len(accept) == 1
    assert grant[0][2] == {"role": "writer", "type": "user",
                           "emailAddress": "thailandnow.info@gmail.com", "pendingOwner": True}
    assert "/permissions/PERM_PEND" in accept[0][1]


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
