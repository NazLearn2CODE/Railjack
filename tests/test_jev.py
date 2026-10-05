"""JEV gauge route — event-loop safety regression (2026-10-05).

The ledger read does a sync httpx.get per JEV_LEDGER_URLS mirror (4s timeout).
Called inline from the async /api/jev route, a dead mirror (orokin offline)
froze the event loop for the whole timeout on EVERY /api/session poll — every
concurrent route (NEWSROOM queue, catalog) stalled with it. The route must run
the read in a worker thread, never on the loop.
"""

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import jev


def test_jev_reads_ledger_off_the_event_loop(monkeypatch):
    """_read_ledger_lines must execute in a worker thread, not the loop thread."""
    seen_threads = []

    def fake_read():
        seen_threads.append(threading.current_thread())
        return [], ["test-ledger"]

    monkeypatch.setattr(jev, "_read_ledger_lines", fake_read)
    app = FastAPI()
    app.include_router(jev.router)
    c = TestClient(app)

    r = c.get("/api/jev")
    assert r.status_code == 200
    assert r.json()["sources"] == ["test-ledger"]
    assert len(seen_threads) == 1
    assert seen_threads[0] is not threading.main_thread(), (
        "ledger read ran on the main/loop thread — a dead mirror will freeze "
        "every concurrent request (the 2026-10-05 QUEUE stall regression)"
    )
