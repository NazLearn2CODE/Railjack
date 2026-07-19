"""Phase E reverse proxy (``/n8n/*`` → n8n root, prefix stripped).

No real n8n/container needed: a stdlib ``http.server`` stub stands in as the
upstream and echoes the path + whether ``Accept-Encoding`` reached it. That lets
us assert the three load-bearing mechanics that broke during development and
must not regress —
  * the ``/n8n`` prefix is stripped (assets live at root on n8n),
  * ``Accept-Encoding`` is dropped so n8n serves identity (no raw gzip stream),
  * upstream headers + query string pass through.
"""

import http.server
import threading
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import n8n_proxy


class _Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence the test stderr noise
        pass

    def _answer(self):
        ae = self.headers.get("accept-encoding", "<none>")
        body = f"PATH={self.path}\nAE={ae}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("X-Stub", "yes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _answer


@contextmanager
def _upstream():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _app() -> FastAPI:
    a = FastAPI()
    a.include_router(n8n_proxy.router)
    return a


def test_strips_prefix_and_forces_identity(monkeypatch):
    with _upstream() as base:
        monkeypatch.setattr(n8n_proxy, "N8N_UPSTREAM", base)
        r = TestClient(_app()).get(
            "/n8n/assets/index-ABC.js", headers={"Accept-Encoding": "gzip, br"}
        )
        assert r.status_code == 200
        assert "PATH=/assets/index-ABC.js" in r.text  # /n8n prefix stripped
        assert "AE=identity" in r.text  # forced identity → n8n won't gzip
        assert r.headers["x-stub"] == "yes"  # upstream headers survive


def test_root_maps_to_upstream_root(monkeypatch):
    with _upstream() as base:
        monkeypatch.setattr(n8n_proxy, "N8N_UPSTREAM", base)
        r = TestClient(_app()).get("/n8n/")
        assert r.status_code == 200
        assert "PATH=/\n" in r.text  # editor shell → n8n root


def test_query_string_forwarded(monkeypatch):
    with _upstream() as base:
        monkeypatch.setattr(n8n_proxy, "N8N_UPSTREAM", base)
        r = TestClient(_app()).get("/n8n/rest/foo?x=1&y=2")
        assert r.status_code == 200
        assert "PATH=/rest/foo?x=1&y=2" in r.text
