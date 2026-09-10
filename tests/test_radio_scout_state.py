"""RADIO scout dispatch badges — per-lane persisted SCOUTED time (Naz 2026-09-10).

The IDE SCOUT / SCOUT buttons must stamp the dispatch time per lane
(global/business) so the cockpit pill survives reloads — parity with
Somatic 37531e3, Railjack-native implementation.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import newsroom


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(newsroom, "_SCOUT_STATE_PATH", tmp_path / "radio_scout_state.json")
    app = FastAPI()
    app.include_router(newsroom.router)
    return TestClient(app)


def test_mark_then_state_roundtrip(client):
    r = client.post("/api/newsroom/radio/scout-mark", json={"lane": "global"})
    assert r.status_code == 200
    assert "scouted_at" in r.json()
    assert r.json()["lane"] == "global"

    s = client.get("/api/newsroom/radio/scout-state")
    assert s.status_code == 200
    lanes = s.json()["lanes"]
    assert lanes["global"]["scouted_at"]  # HH:MM stamped
    assert "business" not in lanes  # untouched lane stays absent


def test_both_lanes_independent(client):
    client.post("/api/newsroom/radio/scout-mark", json={"lane": "global"})
    client.post("/api/newsroom/radio/scout-mark", json={"lane": "business"})
    lanes = client.get("/api/newsroom/radio/scout-state").json()["lanes"]
    assert set(lanes) == {"global", "business"}
    assert lanes["global"]["scouted_at"] != "" and lanes["business"]["scouted_at"] != ""


def test_unknown_lane_rejected(client):
    r = client.post("/api/newsroom/radio/scout-mark", json={"lane": "sports"})
    assert r.status_code == 400


def test_state_file_roundtrip(client, tmp_path):
    # persistence = the JSON file itself: a fresh read of the same file
    # (what a restarted hub would load) must see the stamp.
    client.post("/api/newsroom/radio/scout-mark", json={"lane": "business"})
    import json

    state = json.loads((tmp_path / "radio_scout_state.json").read_text())
    assert state["business"]["scouted_at"]
