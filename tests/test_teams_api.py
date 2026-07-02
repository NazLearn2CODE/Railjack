"""Gateway surface for the Centralized topology: POST /api/teams.

The endpoint builds a Team, hires roles (default or supplied), spawns the
supervisor, and registers it — so the shared /ws/sessions/{id} + /approve +
GET detail surface drives the supervisor like any single-agent run. Construction
is network-free, so this is fully SDK-free testable here; the supervisor *LLM
choosing* to call delegate_many remains the real-LLM integration boundary (see ADR
2026-07-02-centralized-2dot-topology, test_orchestrator.py).

Run: .venv/bin/python -m pytest tests/test_teams_api.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.main as main  # module ref (not a bound import): test_health_sandbox reloads app.main,
# which swaps the module-level `manager` object — a bound `from app.main import manager` would
# point at the stale pre-reload instance. Reading `main.manager` always sees the current one.
from app.core.orchestrator import DEFAULT_ROLES, default_supervisor_prompt


@pytest.fixture()
def client():
    return TestClient(main.app)


def test_create_team_default_roles_returns_supervisor(client):
    r = client.post("/api/teams", json={"prompt": "ship the feature"})
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "supervisor"
    assert len(data["session_id"]) == 36 and data["session_id"].count("-") == 4  # valid UUID (CLI rejects prefixed ids)
    assert data["roles"] == [r.name for r in DEFAULT_ROLES]  # researcher + coder
    # Registered → reachable via the shared session surface, kind preserved.
    g = client.get(f"/api/sessions/{data['session_id']}").json()
    assert g["kind"] == "supervisor"
    assert g["prompt"] == "ship the feature"


def test_create_team_custom_roles_override_default(client):
    r = client.post("/api/teams", json={
        "prompt": "x",
        "roles": [{"name": "qa", "system_prompt": "break things"}],
    })
    assert r.json()["roles"] == ["qa"]


def test_supervisor_carries_delegate_many_tool(client):
    """Construction-time guarantee: the registered supervisor has the delegate_many tool."""
    data = client.post("/api/teams", json={"prompt": "x"}).json()
    sup = main.manager.get_session(data["session_id"])
    assert sup is not None
    assert "delegate_many" in sup.allowed_tools
    assert sup.kind == "supervisor"


def test_default_supervisor_prompt_names_every_role():
    # pure function — a custom roster is reflected, not the baked-in default.
    from app.core.orchestrator import WorkerRole
    roles = [WorkerRole(name="alpha", system_prompt="a"), WorkerRole(name="beta", system_prompt="b")]
    prompt = default_supervisor_prompt(roles)
    assert "alpha" in prompt and "beta" in prompt
    assert "delegate_many" in prompt


if __name__ == "__main__":
    c = TestClient(main.app)
    test_create_team_default_roles_returns_supervisor(c)
    test_create_team_custom_roles_override_default(c)
    test_supervisor_carries_delegate_many_tool(c)
    test_default_supervisor_prompt_names_every_role()
    print("teams-api self-checks: OK")
