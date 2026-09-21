"""Tests for Railjack PROJECTS panel (PM module) backend."""

from __future__ import annotations

from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.projects import (
    MilestoneItem,
    infer_health,
    infer_next_action,
    infer_phase,
    infer_progress,
    infer_repo_type,
    inspect_project,
    parse_brief_status,
    parse_checklist,
    parse_codecompass,
    parse_decisions,
    parse_frontmatter,
    parse_gates,
    parse_index_md,
    parse_sessions,
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ── Parser Unit Tests ────────────────────────────────────────────────────────


def test_parse_frontmatter() -> None:
    content = """---
title: Test Title
status: concept
tags: [game, active]
---
# Body header
Some text here.
"""
    fm, body = parse_frontmatter(content)
    assert fm["title"] == "Test Title"
    assert fm["status"] == "concept"
    assert fm["tags"] == ["game", "active"]
    assert "# Body header" in body

    # No frontmatter
    no_fm, no_body = parse_frontmatter("Just plain text")
    assert no_fm == {}
    assert no_body == "Just plain text"


def test_parse_index_md_milestones_and_status() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "index.md"
        p.write_text("""---
title: My Game
project: My Game Title
status: concept
---
# My Game Index

- **Phase:** see [[gamedev-phases]] — current: **concept**

## Current status

- Working on: Core mechanics prototype
- Next up: Vertical slice playable
- Last build: none (concept phase)

## Milestone table

| ✅ | Milestone | Phase | Date | Commit |
|---|---|---|---|---|
| ✅ | Concept approved | concept | 2026-09-01 | a1b2c3d |
| ☐ | Vertical slice playable | pre-production | | |
| ☐ | Content complete | production | | |
""", encoding="utf-8")

        data = parse_index_md(p)
        assert data["exists"] is True
        assert data["frontmatter"]["project"] == "My Game Title"
        assert data["working_on"] == "Core mechanics prototype"
        assert data["next_up"] == "Vertical slice playable"
        assert data["phase_prose"] == "concept"
        assert len(data["milestones"]) == 3

        m0 = data["milestones"][0]
        assert m0.checked is True
        assert m0.name == "Concept approved"
        assert m0.phase == "concept"
        assert m0.commit == "a1b2c3d"

        m1 = data["milestones"][1]
        assert m1.checked is False
        assert m1.name == "Vertical slice playable"
        assert m1.phase == "pre-production"


def test_parse_index_md_railjack_style_table() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "index.md"
        p.write_text("""---
title: Tool Index
---
## Milestones

| # | Scope | Status |
|---|-------|--------|
| M0 | Setup skeleton | ✅ 2026-07-18 (`028458e`) |
| M1 | Wire API router | ✅ 2026-07-19 |
| M2 | Build frontend UI | ☐ In progress |
""", encoding="utf-8")

        data = parse_index_md(p)
        assert len(data["milestones"]) == 3
        assert data["milestones"][0].checked is True
        assert data["milestones"][1].checked is True
        assert data["milestones"][2].checked is False


def test_parse_codecompass() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)

        # Missing
        state, blocker = parse_codecompass(p)
        assert state == "missing"
        assert blocker is None

        # Template unfilled (GymShelf style)
        cc = p / "CodeCompass.md"
        cc.write_text("""# Code Compass
<!-- One paragraph: the problem it solves and who pays for it. -->

## Current State

**Shipped:** <!-- what's live in production -->
**In progress:** <!-- what's being built right now -->
**Blocked:** <!-- what's stuck, and on what -->
""", encoding="utf-8")
        state, blocker = parse_codecompass(p)
        assert state == "template-unfilled"
        assert blocker is None

        # Filled with blocker
        cc.write_text("""# Code Compass
## Current State

**Shipped:** v1 core engine
**In progress:** Android port
**Blocked:** Waiting on Android SDK install
""", encoding="utf-8")
        state, blocker = parse_codecompass(p)
        assert state == "filled"
        assert blocker == "Waiting on Android SDK install"


def test_parse_gates() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)

        # Missing
        has_gates, gates = parse_gates(p)
        assert has_gates is False
        assert gates == []

        # Present
        gf = p / "gates.yml"
        gf.write_text("""gates:
  - name: lint
    cmd: ruff check .
  - name: test
    cmd: pytest -q
""", encoding="utf-8")
        has_gates, gates = parse_gates(p)
        assert has_gates is True
        assert len(gates) == 2
        assert gates[0].name == "lint"
        assert gates[0].cmd == "ruff check ."


def test_parse_decisions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        d_dir = p / "A-project" / "decisions"
        d_dir.mkdir(parents=True)

        (d_dir / "template.md").write_text("# Template", encoding="utf-8")
        (d_dir / "2026-09-01-choose-engine.md").write_text("""---
title: Choose Godot 4
status: accepted
---
# Choose Godot
""", encoding="utf-8")
        (d_dir / "2026-09-10-camera-perspective.md").write_text("""---
title: 2.5D Camera
status: proposed
---
# Camera
""", encoding="utf-8")

        decisions = parse_decisions(p)
        assert len(decisions) == 2
        assert decisions[0].date == "2026-09-10"
        assert decisions[0].title == "2.5D Camera"
        assert decisions[1].date == "2026-09-01"
        assert decisions[1].status == "accepted"


def test_parse_sessions_and_hot_cache() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        s_dir = p / "B-sessions"
        s_dir.mkdir(parents=True)

        (s_dir / "hot.md").write_text("""---
status: hot
---
## Now
- Phase: **concept**

## Next up
- Pre-production vertical slice

## Handoffs / blockers
- Engine undecided (S1) → gates not runnable yet
""", encoding="utf-8")

        logs, blockers, next_text, next_src, next_quote = parse_sessions(p)
        assert "Engine undecided (S1) → gates not runnable yet" in blockers
        assert next_text == "Pre-production vertical slice"
        assert next_src == "B-sessions/hot.md"
        assert "- Pre-production vertical slice" in next_quote


def test_parse_checklist() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        chk = p / "initial-checklist.md"
        chk.write_text("""# Checklist
- [x] Obsidian setup
- [x] Memory system
- [ ] LLM capabilities
- [ ] Deployment
""", encoding="utf-8")

        checked, total, first_un = parse_checklist(p)
        assert checked == 2
        assert total == 4
        assert first_un == "LLM capabilities"


def test_parse_brief_status() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        brief = p / "Sample-BRIEF.md"
        brief.write_text("""# Brief
Status: SHIPPED 2026-09-14 (build d218e9a · validator PASS)
""", encoding="utf-8")

        status_line, fn = parse_brief_status(p)
        assert status_line == "SHIPPED 2026-09-14 (build d218e9a · validator PASS)"
        assert fn == "Sample-BRIEF.md"


# ── Inference Engine Unit Tests ──────────────────────────────────────────────


def test_infer_repo_type() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        # Plain dir is tool
        assert infer_repo_type(p) == "tool"

        # Game with art-bible.md
        (p / "A-project").mkdir()
        (p / "A-project" / "art-bible.md").write_text("art law", encoding="utf-8")
        assert infer_repo_type(p) == "game"


def test_infer_phase() -> None:
    # Game phase from frontmatter
    phase = infer_phase(
        repo_type="game",
        has_protocol=True,
        is_archived=False,
        index_fm={"status": "concept"},
        phase_prose=None,
        milestones=[],
        checklist_counts=(0, 0),
        brief_status=None,
        days_since_commit=2,
    )
    assert phase == "concept"

    # Tool phase from brief SHIPPED
    phase_tool = infer_phase(
        repo_type="tool",
        has_protocol=True,
        is_archived=False,
        index_fm={},
        phase_prose=None,
        milestones=[],
        checklist_counts=(0, 0),
        brief_status="SHIPPED 2026-09-14 · validator PASS",
        days_since_commit=1,
    )
    assert phase_tool == "validated/shipped"

    # No protocol
    phase_none = infer_phase(
        repo_type="tool",
        has_protocol=False,
        is_archived=False,
        index_fm={},
        phase_prose=None,
        milestones=[],
        checklist_counts=(0, 0),
        brief_status=None,
        days_since_commit=0,
    )
    assert phase_none == "no-protocol"


def test_infer_progress() -> None:
    # Milestone-based
    m_list = [
        MilestoneItem(checked=True, name="M1"),
        MilestoneItem(checked=False, name="M2"),
        MilestoneItem(checked=False, name="M3"),
        MilestoneItem(checked=False, name="M4"),
    ]
    pct, basis = infer_progress("build", m_list, (0, 0))
    assert pct == 25
    assert basis == "milestones (1/4)"

    # Checklist-based
    pct_chk, basis_chk = infer_progress("bootstrap", [], (5, 10))
    assert pct_chk == 50
    assert basis_chk == "checklist (5/10)"

    # Coarse estimate
    pct_est, basis_est = infer_progress("production", [], (0, 0))
    assert pct_est == 50
    assert "est." in basis_est

    # No protocol
    pct_no, basis_no = infer_progress("no-protocol", [], (0, 0))
    assert pct_no is None
    assert basis_no == "no protocol"


def test_infer_health() -> None:
    # Green: commit <= 14d, no blockers
    h_green = infer_health(
        phase="build",
        days_since_commit=3,
        has_uncommitted=False,
        codecompass_state="filled",
        explicit_compass_blocker=None,
        blockers=[],
        brief_status=None,
    )
    assert h_green == "green"

    # Amber: uncommitted changes
    h_amber_dirty = infer_health(
        phase="build",
        days_since_commit=3,
        has_uncommitted=True,
        codecompass_state="filled",
        explicit_compass_blocker=None,
        blockers=[],
        brief_status=None,
    )
    assert h_amber_dirty == "amber"

    # Amber: blockers present
    h_amber_block = infer_health(
        phase="concept",
        days_since_commit=1,
        has_uncommitted=False,
        codecompass_state="missing",
        explicit_compass_blocker=None,
        blockers=["Engine undecided"],
        brief_status=None,
    )
    assert h_amber_block == "amber"

    # Amber: template-unfilled CodeCompass
    h_amber_cc = infer_health(
        phase="build",
        days_since_commit=2,
        has_uncommitted=False,
        codecompass_state="template-unfilled",
        explicit_compass_blocker=None,
        blockers=[],
        brief_status=None,
    )
    assert h_amber_cc == "amber"

    # Red: explicit blocker
    h_red_block = infer_health(
        phase="build",
        days_since_commit=2,
        has_uncommitted=False,
        codecompass_state="filled",
        explicit_compass_blocker="Blocked by payment API",
        blockers=[],
        brief_status=None,
    )
    assert h_red_block == "red"

    # Red: validator FAIL
    h_red_fail = infer_health(
        phase="build",
        days_since_commit=2,
        has_uncommitted=False,
        codecompass_state="filled",
        explicit_compass_blocker=None,
        blockers=[],
        brief_status="build broken · validator FAIL",
    )
    assert h_red_fail == "red"

    # Grey: no protocol
    h_grey = infer_health(
        phase="no-protocol",
        days_since_commit=2,
        has_uncommitted=False,
        codecompass_state="missing",
        explicit_compass_blocker=None,
        blockers=[],
        brief_status=None,
    )
    assert h_grey == "grey"


def test_infer_next_action_ladder() -> None:
    # 1. Compass blocker wins over everything
    na1 = infer_next_action(
        phase="build",
        explicit_compass_blocker="Missing API key",
        brief_status=None,
        session_next_up=("Do something", "session.md", "- Do something"),
        index_next_up="Next up index",
        milestones=[],
        first_unchecked_chk=None,
        days_since_commit=1,
        last_commit=None,
    )
    assert "Missing API key" in na1.text
    assert na1.source_file == "CodeCompass.md"

    # 2. Session next up wins when no blocker
    na2 = infer_next_action(
        phase="build",
        explicit_compass_blocker=None,
        brief_status=None,
        session_next_up=("Naz review", "B-sessions/hot.md", "- Naz review"),
        index_next_up="Index next up",
        milestones=[],
        first_unchecked_chk=None,
        days_since_commit=1,
        last_commit=None,
    )
    assert na2.text == "Naz review"
    assert na2.source_file == "B-sessions/hot.md"

    # 3. Index next up wins when session empty
    na3 = infer_next_action(
        phase="build",
        explicit_compass_blocker=None,
        brief_status=None,
        session_next_up=(None, None, None),
        index_next_up="Index next up",
        milestones=[],
        first_unchecked_chk=None,
        days_since_commit=1,
        last_commit=None,
    )
    assert na3.text == "Index next up"
    assert na3.source_file == "A-project/index.md"

    # 4. Milestone first unchecked
    m_list = [
        MilestoneItem(checked=True, name="Done M1"),
        MilestoneItem(checked=False, name="Next M2", phase="beta"),
    ]
    na4 = infer_next_action(
        phase="build",
        explicit_compass_blocker=None,
        brief_status=None,
        session_next_up=(None, None, None),
        index_next_up=None,
        milestones=m_list,
        first_unchecked_chk=None,
        days_since_commit=1,
        last_commit=None,
    )
    assert na4.text == "Next M2"
    assert na4.source_file == "A-project/index.md"

    # 5. Fallback for no-protocol
    na5 = infer_next_action(
        phase="no-protocol",
        explicit_compass_blocker=None,
        brief_status=None,
        session_next_up=(None, None, None),
        index_next_up=None,
        milestones=[],
        first_unchecked_chk=None,
        days_since_commit=1,
        last_commit=None,
    )
    assert "stamp via project-bootstrap" in na5.text


# ── Real Fixture Integration Tests ───────────────────────────────────────────


@pytest.mark.anyio
async def test_real_fixtures_on_disk() -> None:
    """Validate behavior against the real ~/GameDev/payont-siam and ~/GameDev/protocol fixtures."""
    payont_dir = Path("/home/naz6395/GameDev/payont-siam")
    protocol_dir = Path("/home/naz6395/GameDev/protocol")

    if not payont_dir.exists() or not protocol_dir.exists():
        pytest.skip("GameDev fixtures not present on this machine")

    # 1. payont-siam fixture
    payont = await inspect_project(payont_dir)
    assert payont.id == "payont-siam"
    assert payont.name == "Payont Siam"
    assert payont.repo_type == "game"
    # Real fixture advances as Naz gates phases and checks milestones
    # (concept → pre-production 2026-09-21) — pin vocabulary and internal
    # consistency, not snapshot values.
    assert payont.phase in {"concept", "pre-production", "production", "alpha", "beta", "gold", "live-ops"}
    assert payont.protocol.index is True
    assert payont.protocol.gates is True
    assert len(payont.milestones) == 7
    m_checked = sum(1 for m in payont.milestones if m.checked)
    assert payont.pct == round((m_checked / 7) * 100)
    assert payont.pct_basis == f"milestones ({m_checked}/7)"
    assert payont.last_commit is not None
    assert "b370795" in payont.last_commit.hash or len(payont.last_commit.hash) >= 7
    # Blockers churn with project reality (Engine-undecided resolved when
    # Godot was picked) — assert shape, not specific blockers.
    assert isinstance(payont.blockers, list) and all(isinstance(b, str) and b for b in payont.blockers)
    assert payont.health in ("amber", "green")
    assert bool(payont.next_action.text)

    # 2. protocol fixture (no A-project -> no-protocol state, never 500)
    prot = await inspect_project(protocol_dir)
    assert prot.id == "protocol"
    assert prot.phase == "no-protocol"
    assert prot.health == "grey"
    assert prot.pct is None
    assert prot.pct_basis == "no protocol"
    assert prot.protocol.index is False
    assert prot.protocol.gates is False
    assert "project-bootstrap" in prot.next_action.text


# ── API Endpoint Tests ───────────────────────────────────────────────────────


def test_api_projects_summary(client: TestClient) -> None:
    res = client.get("/api/projects/summary")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    # Check that discovered projects contain payont-siam and protocol
    ids = [p["id"] for p in data]
    assert "payont-siam" in ids
    assert "protocol" in ids

    # Check summary item schema
    payont = next(p for p in data if p["id"] == "payont-siam")
    assert payont["phase"] in {"concept", "pre-production", "production", "alpha", "beta", "gold", "live-ops"}
    assert "trend_sparkline" in payont
    assert len(payont["trend_sparkline"]) == 14
    assert "next_action" in payont
    assert "text" in payont["next_action"]
    assert "source_file" in payont["next_action"]


def test_api_projects_detail(client: TestClient) -> None:
    res = client.get("/api/projects/payont-siam")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "payont-siam"
    assert data["repo_type"] == "game"
    assert len(data["milestones"]) > 0
    assert len(data["gates"]) > 0
    assert len(data["git_log"]) > 0

    # 404 on nonexistent
    res_404 = client.get("/api/projects/nonexistent-project-xyz")
    assert res_404.status_code == 404


def test_api_projects_rescan(client: TestClient) -> None:
    res = client.post("/api/projects/rescan")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["count"] >= 2
    assert isinstance(data["projects"], list)
