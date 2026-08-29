"""M6 cockpit — terminal insert, catalog grouping, session block math.

Stdlib + fastapi TestClient only; no new deps. tmux and the filesystem are
mocked (monkeypatch the runner / point module path constants at tmp_path) —
no real tmux, no real ~/.claude.
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import catalog, terminal_input
from app.config import (
    CatalogGroup,
    CatalogSpec,
    MachineConfig,
    Module,
    Provider,
)


# ---------------------------------------------------------------- helpers


def _mk_session():
    """Build a MachineConfig whose catalog drives grouping + template."""
    return MachineConfig(
        machine="x",
        hostnames=["x"],
        modules=[Module(id="tmux", title="T", kind="iframe", options={"tmux_session": "main"})],
        providers=[Provider(name="anthropic", model_match="^claude-", window_hours=5, context_limit=200_000)],
        catalog=CatalogSpec(
            mcp_insert_template="use the {name} MCP to ",
            groups=[
                CatalogGroup(name="F5", match="^f5-"),
                CatalogGroup(name="MEDIA", match="comfyui|ffmpeg|davinci|youtube|story|channel|case|resolve"),
                CatalogGroup(name="RESEARCH", match="reach|search|reader|zread|newstank|notebooklm|radar"),
                CatalogGroup(name="AGENTS", match="agent|subagent|delegate|llm"),
                CatalogGroup(name="WORKSPACE", match="google|obsidian|workspace"),
            ],
        ),
    )


# ---------------------------------------------------------------- terminal insert


def _client(monkeypatch):
    app = FastAPI()
    app.include_router(terminal_input.router)
    monkeypatch.setattr(
        terminal_input, "CONFIG",
        MachineConfig(machine="x", hostnames=["x"],
                      modules=[Module(id="tmux", title="T", kind="iframe",
                                      options={"tmux_session": "main"})]),
    )
    return TestClient(app)


def test_insert_rejects_newline(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/terminal/insert", json={"text": "ls\nwhoami"}).status_code == 400
    assert c.post("/api/terminal/insert", json={"text": "a\rb"}).status_code == 400


def test_insert_rejects_oversize(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/terminal/insert", json={"text": "x" * 4001}).status_code == 400


def test_insert_happy_path_argv(monkeypatch):
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return (0, "", "")

    monkeypatch.setattr(terminal_input, "_run", fake_run)
    c = _client(monkeypatch)

    r = c.post("/api/terminal/insert", json={"text": "ls -la"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert calls == [["tmux", "send-keys", "-t", "main", "-l", "--", "ls -la"]]


def test_insert_leading_dash_guarded_by_double_dash(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal_input, "_run", lambda argv: (calls.append(argv) or (0, "", "")))
    c = _client(monkeypatch)
    assert c.post("/api/terminal/insert", json={"text": "/f5-vibe-check "}).status_code == 200
    # `--` sits before the text so a leading `/` is treated literally, not a flag
    assert calls[-1] == ["tmux", "send-keys", "-t", "main", "-l", "--", "/f5-vibe-check "]


def test_insert_surfaces_tmux_stderr(monkeypatch):
    monkeypatch.setattr(terminal_input, "_run", lambda argv: (1, "", "no server\n"))
    c = _client(monkeypatch)
    r = c.post("/api/terminal/insert", json={"text": "ls"})
    assert r.status_code == 200
    assert r.json() == {"status": "error", "detail": "no server"}


# ---------------------------------------------------------------- catalog grouping


def _catalog_env(monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    for n in ("f5-comfyui-media", "agent-reach", "notebooklm", "rate-ck"):
        (skills / n).mkdir()
    monkeypatch.setattr(catalog, "SKILLS_DIR", skills)

    # Installed plugin shipping skills → the marketplace-skills menu. Only dirs
    # with a SKILL.md count; "notaskill" is ignored.
    plug = tmp_path / "plug" / "marketing-skills" / "2.5.1"
    for n in ("marketing-plan", "ads"):
        (plug / "skills" / n).mkdir(parents=True)
        (plug / "skills" / n / "SKILL.md").write_text("x")
    (plug / "skills" / "notaskill").mkdir(parents=True)
    ip = tmp_path / "installed_plugins.json"
    ip.write_text(json.dumps({
        "plugins": {
            "marketing-skills@marketingskills": [{"scope": "user", "installPath": str(plug)}],
        },
    }))
    monkeypatch.setattr(catalog, "INSTALLED_PLUGINS_JSON", ip)

    # Project dir with its own .mcp.json (the Cephalon-vault `obsidian` case).
    proj = tmp_path / "vault"
    proj.mkdir()
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"obsidian": {}}}))

    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({
        "mcpServers": {"zai-mcp-server": {}, "web-search-prime": {}},
        "projects": {
            str(proj): {"mcpServers": {"davinci-resolve": {}, "zai-mcp-server": {}}},
        },
    }))
    monkeypatch.setattr(catalog, "CLAUDE_JSON", cj)
    monkeypatch.setattr(catalog, "CONFIG", _mk_session())


def test_catalog_skill_grouping_first_match_wins(monkeypatch, tmp_path):
    _catalog_env(monkeypatch, tmp_path)
    data = catalog._build()
    by_name = {s["name"]: s for s in data["skills"]}
    # f5-comfyui-media matches both ^f5- (F5) and MEDIA(comfyui) → F5 wins
    assert by_name["f5-comfyui-media"]["group"] == "F5"
    assert by_name["f5-comfyui-media"]["insert"] == "/f5-comfyui-media "
    # agent-reach matches RESEARCH(reach) before AGENTS(agent) → RESEARCH wins
    assert by_name["agent-reach"]["group"] == "RESEARCH"
    assert by_name["notebooklm"]["group"] == "RESEARCH"
    assert by_name["rate-ck"]["group"] == "OTHER"  # no rule matches


def test_catalog_marketplace_skills_grouped_by_plugin(monkeypatch, tmp_path):
    _catalog_env(monkeypatch, tmp_path)
    data = catalog._build()
    ms = data["marketplace_skills"]
    # sorted within plugin; "notaskill" (no SKILL.md) excluded
    assert [m["name"] for m in ms] == ["ads", "marketing-plan"]
    # grouped by uppercased plugin name; fully-qualified /plugin:name insert
    assert all(m["group"] == "MARKETING-SKILLS" for m in ms)
    assert {m["insert"] for m in ms} == {
        "/marketing-skills:ads ",
        "/marketing-skills:marketing-plan ",
    }


def test_catalog_marketplace_skills_missing_file_empty(monkeypatch, tmp_path):
    _catalog_env(monkeypatch, tmp_path)
    monkeypatch.setattr(catalog, "INSTALLED_PLUGINS_JSON", tmp_path / "nope.json")
    assert catalog._build()["marketplace_skills"] == []


def test_catalog_mcp_dedup_template_grouping(monkeypatch, tmp_path):
    _catalog_env(monkeypatch, tmp_path)
    data = catalog._build()
    mcps = data["mcps"]
    names = [m["name"] for m in mcps]
    # global keys first, then project-scope (~/.claude.json), then the
    # project's own .mcp.json; zai-mcp-server appears once (deduped)
    assert names == ["zai-mcp-server", "web-search-prime", "davinci-resolve", "obsidian"]
    by_name = {m["name"]: m for m in mcps}
    # template substitution
    assert by_name["zai-mcp-server"]["insert"] == "use the zai-mcp-server MCP to "
    # grouping
    assert by_name["web-search-prime"]["group"] == "RESEARCH"  # 'search'
    assert by_name["davinci-resolve"]["group"] == "MEDIA"  # 'davinci'/'resolve'
    assert by_name["zai-mcp-server"]["group"] == "OTHER"  # no rule matches


def test_catalog_reload_busts_cache(monkeypatch, tmp_path):
    """POST /api/catalog/reload rescans the filesystem even inside the 60 s cache
    window — the mechanism behind the ↻ CFG button surfacing a just-added skill."""
    _catalog_env(monkeypatch, tmp_path)
    app = FastAPI()
    app.include_router(catalog.router)
    c = TestClient(app)

    # Prime the cache with a GET, then add a skill on disk. A second GET still
    # serves the stale cache (this is exactly the bug the reload fixes).
    assert "brand-new" not in [s["name"] for s in c.get("/api/catalog").json()["skills"]]
    (catalog.SKILLS_DIR / "brand-new").mkdir()
    assert "brand-new" not in [s["name"] for s in c.get("/api/catalog").json()["skills"]]

    # Reload busts the cache → the new skill appears, and stays cached for GETs.
    reloaded = c.post("/api/catalog/reload").json()
    assert "brand-new" in [s["name"] for s in reloaded["skills"]]
    assert "brand-new" in [s["name"] for s in c.get("/api/catalog").json()["skills"]]
