"""M6 cockpit — terminal insert, catalog grouping, session block math.

Stdlib + fastapi TestClient only; no new deps. tmux and the filesystem are
mocked (monkeypatch the runner / point module path constants at tmp_path) —
no real tmux, no real ~/.claude.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import catalog, session_stats, terminal_input
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


# ---------------------------------------------------------------- block math


def _dt(h, m=0):
    return datetime(2026, 7, 18, h, m, tzinfo=timezone.utc)


def test_block_floor_to_hour_single_event():
    start, reset = session_stats._current_block([_dt(10, 35)], 5)
    assert start == _dt(10)
    assert reset == _dt(15)


def test_block_no_gap_stays_in_one_block():
    ev = [_dt(10, 35), _dt(11, 20), _dt(12, 0)]
    start, reset = session_stats._current_block(ev, 5)
    assert start == _dt(10)
    assert reset == _dt(15)  # 10:00 + 5h


def test_block_gap_starts_new_block():
    # 5.5h gap → second event opens a new block, floored to its hour
    ev = [_dt(10, 30), _dt(16, 0)]
    start, reset = session_stats._current_block(ev, 5)
    assert start == _dt(16)
    assert reset == _dt(21)


def test_block_dense_span_past_reset_starts_new_block():
    # no single gap ≥5h, but the run crosses reset_at (10:00+5h=15:00)
    ev = [_dt(10, 30), _dt(14, 55), _dt(15, 5)]
    start, reset = session_stats._current_block(ev, 5)
    assert start == _dt(15)
    assert reset == _dt(20)


def test_block_empty_returns_none():
    assert session_stats._current_block([], 5) == (None, None)


# ---------------------------------------------------------------- provider match


def _providers():
    return [
        Provider(name="anthropic", model_match="^claude-", window_hours=5, context_limit=200_000),
        Provider(name="zai", model_match="^glm-", window_hours=5, context_limit=200_000),
    ]


def test_provider_match_anthropic():
    p, rx = session_stats._match_provider("claude-fable-5", _providers())
    assert p is not None and p.name == "anthropic"
    assert rx is not None and rx.search("claude-x")
    assert (p.window_hours, p.context_limit) == (5, 200_000)


def test_provider_match_zai():
    p, rx = session_stats._match_provider("glm-5", _providers())
    assert p is not None and p.name == "zai"
    assert rx is not None and rx.search("glm-5") and not rx.search("claude-5")


def test_provider_unknown_model_fallback():
    p, rx = session_stats._match_provider("gpt-4o", _providers())
    assert p is None  # caller substitutes defaults
    assert rx is None  # no sibling files contribute to block math


def test_provider_none_model_fallback():
    p, rx = session_stats._match_provider(None, _providers())
    assert p is None and rx is None


# -------------------------------------------------------- per-model context limit
def test_context_limit_resolves_glm52_to_1m():
    # The reported bug: config said 200k, real limit is 1M → gauge was 5× off.
    assert session_stats._resolve_context_limit("glm-5.2", 200_000) == 1_000_000


def test_context_limit_claude_family_200k():
    assert session_stats._resolve_context_limit("claude-opus-4-8", 200_000) == 200_000


def test_context_limit_unknown_model_uses_fallback():
    assert session_stats._resolve_context_limit("gpt-4o", 128_000) == 128_000


def test_context_limit_none_model_uses_fallback():
    assert session_stats._resolve_context_limit(None, 200_000) == 200_000


def test_session_clamps_stale_table_entry_below_observed_context(monkeypatch, tmp_path):
    """If a model accepts more than the table/limit claims (proof its real limit
    is ≥ ctx), the gauge denominator rises to ctx so it never reads >100 %."""
    root = tmp_path / "projects" / "slug"
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    # glm-5-turbo isn't in the table → falls back to provider's 200k, but ctx
    # (350k) exceeds it: clamp must lift the denominator to 350k → 100 %.
    line = {
        "type": "assistant",
        "timestamp": now.isoformat(),
        "message": {"model": "glm-5-turbo",
                    "usage": {"input_tokens": 350_000, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0, "output_tokens": 0}},
    }
    (root / "abc.jsonl").write_text(json.dumps(line))
    monkeypatch.setattr(session_stats, "SESSIONS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(session_stats, "CONFIG", _mk_session())

    st = session_stats._session_state()
    assert st["context_tokens"] == 350_000
    assert st["context_limit"] == 350_000  # clamped up from 200k
    assert st["context_pct"] == 100


# ---------------------------------------------------------------- session end-to-end


def test_session_state_parses_newest_transcript(monkeypatch, tmp_path):
    root = tmp_path / "projects" / "slug"
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc)

    def _iso(dt):
        return dt.isoformat()

    lines = [
        # user/tool lines contribute timestamps (block activity)
        {"type": "user", "timestamp": _iso(now - timedelta(minutes=40))},
        {"type": "assistant", "timestamp": _iso(now - timedelta(minutes=35)),
         "message": {"model": "claude-fable-5",
                     "usage": {"input_tokens": 2, "cache_read_input_tokens": 104314,
                               "cache_creation_input_tokens": 433, "output_tokens": 280}}},
        {"type": "user", "timestamp": _iso(now - timedelta(minutes=5))},
    ]
    (root / "abc.jsonl").write_text("\n".join(json.dumps(o) for o in lines))

    monkeypatch.setattr(session_stats, "SESSIONS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(session_stats, "CONFIG", _mk_session())

    st = session_stats._session_state()
    assert st["provider"] == "anthropic"
    assert st["model"] == "claude-fable-5"
    # input (2 + 104314 + 433) + output (280) = post-turn context fill
    assert st["context_tokens"] == 2 + 104314 + 433 + 280
    assert st["context_limit"] == 200_000
    assert st["context_pct"] == round((105029 / 200_000) * 100)  # 53
    assert st["idle"] is False  # fresh mtime
    # block start floored to the hour of the first (40m-ago) event
    assert st["reset_at"] is not None
    # no usage_source configured → heuristic fallback is marked as such
    assert st["source"] == "estimate"
    assert st["session_pct"] is None


def test_session_state_no_files_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(session_stats, "SESSIONS_ROOT", tmp_path)  # empty
    st = session_stats._session_state()
    assert st["idle"] is True
    assert st["provider"] == "?"
    assert st["context_tokens"] == 0


# ---------------------------------------------------------------- usage adapters (M6.1)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_usage_anthropic_parses(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-not-logged"}}))
    monkeypatch.setattr(session_stats, "CREDS_FILE", creds)
    payload = {
        "five_hour": {"utilization": 20.0, "resets_at": "2026-07-18T05:19:59+00:00"},
        "seven_day": {"utilization": 42.0, "resets_at": "2026-07-19T22:59:59+00:00"},
    }
    monkeypatch.setattr(session_stats.httpx, "get", lambda *a, **k: _FakeResp(payload))
    u = session_stats._usage_anthropic()
    assert u == {
        "session_pct": 20,
        "weekly_pct": 42,
        "reset_at": "2026-07-18T05:19:59+00:00",
    }


def test_usage_anthropic_missing_creds_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(session_stats, "CREDS_FILE", tmp_path / "nope.json")
    assert session_stats._usage_anthropic() is None


def test_usage_zai_parses(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "key-not-logged")
    # rate-ck shape: TOKENS_LIMIT is the binding quota; TIME_LIMIT is ignored.
    payload = {
        "data": {
            "limits": [
                {"type": "TIME_LIMIT", "percentage": 68, "nextResetTime": 1785558806983},
                {"type": "TOKENS_LIMIT", "percentage": 50, "nextResetTime": 1784336157957},
            ]
        }
    }
    monkeypatch.setattr(session_stats.httpx, "get", lambda *a, **k: _FakeResp(payload))
    u = session_stats._usage_zai("ZAI_API_KEY")
    assert u["session_pct"] == 50
    assert u["weekly_pct"] is None
    # 1784336157957 ms → aware UTC iso
    assert u["reset_at"].startswith("2026-07-18T")


def test_usage_zai_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    assert session_stats._usage_zai("ZAI_API_KEY") is None


def test_session_state_prefers_api_over_heuristic(monkeypatch, tmp_path):
    root = tmp_path / "projects" / "slug"
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    line = {
        "type": "assistant",
        "timestamp": now.isoformat(),
        "message": {"model": "claude-fable-5", "usage": {"input_tokens": 100}},
    }
    (root / "abc.jsonl").write_text(json.dumps(line))
    monkeypatch.setattr(session_stats, "SESSIONS_ROOT", tmp_path / "projects")

    cfg = _mk_session()
    cfg.providers[0].usage_source = "anthropic-oauth"
    monkeypatch.setattr(session_stats, "CONFIG", cfg)
    monkeypatch.setattr(
        session_stats,
        "_usage_anthropic",
        lambda: {"session_pct": 11, "weekly_pct": 42, "reset_at": "2026-07-18T05:21:00+00:00"},
    )

    st = session_stats._session_state()
    assert st["source"] == "api"
    assert st["session_pct"] == 11
    assert st["weekly_pct"] == 42
    assert st["reset_at"] == "2026-07-18T05:21:00+00:00"
    # context % still comes from the JSONL regardless of usage source
    assert st["context_tokens"] == 100


def test_session_state_api_failure_falls_back(monkeypatch, tmp_path):
    root = tmp_path / "projects" / "slug"
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    line = {
        "type": "assistant",
        "timestamp": now.isoformat(),
        "message": {"model": "claude-fable-5", "usage": {"input_tokens": 100}},
    }
    (root / "abc.jsonl").write_text(json.dumps(line))
    monkeypatch.setattr(session_stats, "SESSIONS_ROOT", tmp_path / "projects")

    cfg = _mk_session()
    cfg.providers[0].usage_source = "anthropic-oauth"
    monkeypatch.setattr(session_stats, "CONFIG", cfg)
    monkeypatch.setattr(session_stats, "_usage_anthropic", lambda: None)  # API down

    st = session_stats._session_state()
    assert st["source"] == "estimate"
    assert st["session_pct"] is None
    assert st["reset_at"] is not None  # heuristic still supplies a reset


def test_fetch_usage_retains_last_good_on_transient_failure(monkeypatch):
    """A transient API failure (e.g. Anthropic /usage 429) reuses the last good
    reading instead of dropping to None, so the strip doesn't flap to estimate."""
    prov = _mk_session().providers[0]
    prov.usage_source = "anthropic-oauth"
    good = {"session_pct": 77, "weekly_pct": 12, "reset_at": "2026-07-18T05:19:59+00:00"}

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return good if calls["n"] == 1 else None  # first ok, then 429s

    monkeypatch.setattr(session_stats, "_usage_anthropic", flaky)
    assert session_stats._fetch_usage(prov) == good  # fresh
    assert session_stats._fetch_usage(prov) == good  # retained across failure
    assert calls["n"] == 2


def test_fetch_usage_drops_after_retention_window(monkeypatch):
    """Past the retention window a persistent failure yields None → estimate."""
    prov = _mk_session().providers[0]
    prov.usage_source = "anthropic-oauth"
    good = {"session_pct": 77, "weekly_pct": 12, "reset_at": "2026-07-18T05:19:59+00:00"}

    seq = [good, None]
    monkeypatch.setattr(session_stats, "_usage_anthropic", lambda: seq.pop(0))
    clock = [1000.0]
    monkeypatch.setattr(session_stats.time, "monotonic", lambda: clock[0])

    assert session_stats._fetch_usage(prov) == good
    clock[0] += session_stats._USAGE_RETAIN + 1  # age the cached reading out
    assert session_stats._fetch_usage(prov) is None


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
    assert c.post("/api/terminal/insert", json={"text": "x" * 501}).status_code == 400


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
