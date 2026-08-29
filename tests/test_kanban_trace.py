"""Unit tests for SSSF auto-dispatch tracer module and Kanban trace endpoint."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.factory.tracer import get_tracer, set_tracer_db_path, trace_for_task
from app.kanban import _watchdog_kill, router

_test_app = FastAPI()
_test_app.include_router(router)


@pytest.fixture(autouse=True)
def temp_tracer_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    db_path = Path(tmp_dir) / "test_sssf.db"
    set_tracer_db_path(db_path)
    yield db_path
    set_tracer_db_path(None)


def test_tracer_crud_and_query():
    tr = get_tracer()
    adw_id = "task-42"
    tr.session_start(adw_id, "Test Title", "Test Description")
    phase_id = tr.phase_start(adw_id, seq=1, name="worker", kind="agent", owner="claude")

    eid = tr.event(adw_id, phase_id, "dispatch", "claude -p", {"prompt_len": 120})
    assert eid.startswith("evt_")

    tr.event(adw_id, phase_id, "activity", "step 1 complete")
    tr.phase_end(phase_id, "pass")
    tr.session_end(adw_id, "accepted")

    data = trace_for_task(42)
    assert data["session"]["adw_id"] == adw_id
    assert data["session"]["status"] == "accepted"
    assert len(data["phases"]) == 1
    assert data["phases"][0]["status"] == "pass"
    assert len(data["events"]) == 2
    assert data["events"][0]["type"] == "dispatch"
    assert data["events"][0]["payload"] == {"prompt_len": 120}
    assert data["events"][1]["type"] == "activity"


def test_watchdog_kill_records_trace():
    adw_id = "task-99"
    tr = get_tracer()
    tr.session_start(adw_id, "Stalled Task", "Running forever")
    phase_id = tr.phase_start(adw_id, seq=1, name="worker", kind="agent", owner="claude")

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    _watchdog_kill(99, mock_proc, adw_id=adw_id, phase_id=phase_id)

    data = trace_for_task(99)
    assert data["session"]["status"] == "failed"
    assert data["phases"][0]["status"] == "fail"
    assert data["phases"][0]["error"] == "watchdog: exceeded max-runtime"

    event_types = [e["type"] for e in data["events"]]
    assert "watchdog_kill" in event_types


def test_trace_api_endpoint():
    client = TestClient(_test_app)

    tr = get_tracer()
    adw_id = "task-77"
    tr.session_start(adw_id, "API Task", "Check route")
    phase_id = tr.phase_start(adw_id, seq=1, name="worker", kind="agent", owner="claude")
    tr.event(adw_id, phase_id, "dispatch", "claude -p")

    resp = client.get("/api/kanban/trace/77")
    assert resp.status_code == 200
    json_data = resp.json()
    assert json_data["session"]["adw_id"] == "task-77"
    assert len(json_data["events"]) == 1
