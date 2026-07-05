# Orbiter — Next Increment: Mission-Control Surface (Tabs · Skills · Cephalon · Provider Switcher · Services · Extension Guide)

## Context

The original project brief asks for a mission-control IDE. Most of it is **already built and live** (chat-style agent UI, WS streaming, teams/fan-out, approvals, security floor, the `Provider` Protocol adapter seam). This plan covers the confirmed **delta**:

1. Left tab bar (AGENTS / SKILLS / SERVICES) — today the left column is only a session list
2. Skills view (available skills + location + currently-active skill per session)
3. Project view with a **Cephalon protocol** compliance indicator
4. Provider/model switcher — scope confirmed as **model picker + registry skeleton** (backend stays z.ai/GLM; a second provider becomes a config edit, not a refactor)
5. Services tab — **launcher tiles** (Gemini/Perplexity/Google Docs block iframes; `embed:true` iframe only for services that allow it, e.g. self-hosted)
6. `A-project/extending.md` — the migration/extension guide

Verified enabler: installed `claude-agent-sdk` `ClaudeAgentOptions` supports `model:` (types.py:1671) **and** per-subprocess `env:` (types.py:1721) — per-session provider routing is a real seam, not speculative.

Product note (one line): Orbiter is a personal local-first tool today; the natural offer if ever productized is open-source + paid hosted/support — no monetization work in this increment (YAGNI).

## Architecture Overview

```
┌──────────────────────────── BROWSER (React 19 + Zustand) ────────────────────────────┐
│ TopBar                                                                               │
│ ┌─ Sidebar ─────────┬─ Console ─────────────────────┬─ Telemetry ──────────────────┐ │
│ │ [AGENTS] SKILLS   │ transcript (Messages, lanes,  │ 02 SESSION (+ SKILL row)     │ │
│ │        SERVICES   │  ApprovalCards)               │ 03 EVENT STREAM              │ │
│ │ session cards /   │  — or ServiceFrame iframe —   │ 04 HOST                      │ │
│ │ skills / tiles    │ Composer (+ MDL dropdown)     │ 05 PROJECT (Cephalon pips)   │ │
│ └───────────────────┴───────────────────────────────┴──────────────────────────────┘ │
└────────────┬── REST ──────────────┬── WS /ws/sessions/{id} ───────────────────────────┘
             ▼                      ▼
┌──────────────────────── FastAPI gateway (app/main.py) ───────────────────────────────┐
│ POST /api/sessions|/api/teams {+model,+provider}   GET /api/skills   GET /api/providers │
│ GET /api/health {+services,+workspace}                                                 │
└───┬───────────────────┬─────────────────────┬─────────────────────────────────────────┘
    ▼                   ▼                     ▼
 app/core/skills.py  app/core/registry.py  app/core/cephalon.py     (all new, pure-python)
 FS scan of skill    ORBITER_PROVIDERS →   probe workspace root:
 dirs + plugins      resolve(name,model)→  CLAUDE.md/CodeCompass/
                     (model, env_override) A-project/obsidian-MCP
                          │
                          ▼
              ClaudeSdkProvider(model=, env=)  ← existing SDK boundary, provider.py:145-198
              → ClaudeAgentOptions(model=…, env=…) → CLI subprocess with per-session env
```

**New files:** `app/core/skills.py`, `app/core/cephalon.py`, `app/core/registry.py`, `web/src/components/SkillsList.tsx`, `ServicesList.tsx`, `ServiceFrame.tsx`, `A-project/extending.md`, tests.
**Changed:** `app/main.py`, `app/core/provider.py`, `app/core/agent.py` (2 lines), `web/src/{Sidebar,Console,Telemetry,Composer}.tsx`, `store.ts`, `api.ts`, `types.ts`.

## Backend design

### Provider registry — `app/core/registry.py` (~60 lines, mirrors `_load_mcp_servers`)
- `ORBITER_PROVIDERS` env = JSON `[{"name", "base_url"?, "auth_env"?, "models": [..]}]`; default `[{"name":"default","models":[]}]` (= ambient shell env, SDK default model). Malformed → log + default.
- `public_view()` → `[{name, models}]` **only** — `base_url`/`auth_env` never serialize (secrets stay server-side; token read from server env named by `auth_env` at spawn time).
- `resolve(name, model)` → `(model, env_overrides | None)`; unknown name/model → HTTP 400 upstream.
- `# ponytail:` env-JSON only, no config file — trigger: >2 providers.

### Provider seam — `provider.py` + `main.py`
- `ClaudeSdkProvider.__init__` gains `model: str|None`, `env: dict|None`; `stream()` sets them on `ClaudeAgentOptions`. **`Provider` Protocol unchanged** (ctor state, like `mcp_servers`). `FakeProvider` untouched.
- `CreateSession`/`CreateTeam` bodies gain optional `model`, `provider`. When set → per-request `ClaudeSdkProvider(mcp_servers=EXTERNAL_MCP, model=…, env=…)`; else the global singleton (main.py:77). Teams: workers share the supervisor's provider.
- `AgentSessionManager.create_session(…, provider: Provider|None = None)` → `provider or self.provider` (agent.py:311).
- New `GET /api/providers` → `public_view(REGISTRY)`.

### Skills — `app/core/skills.py` + `GET /api/skills`
- Stdlib frontmatter parse (plain + `>`-folded description); scan `<workspace>/.claude/skills/*`, `~/.claude/skills/*`, plugin dirs via `~/.claude/plugins/installed_plugins.json` → `[{name, description, source, path}]`; anything unreadable → skip, never raise. Uncached (`# ponytail:` cache when measurably slow).
- **Active skill: zero backend** — frontend derives it from existing transcript rows (last `tool_use` with `name === "Skill"` → `input.skill`); one selector in `util.ts`.

### Cephalon probe — `app/core/cephalon.py`
- `probe(root)` (`lru_cache`; restart re-probes): checks `CLAUDE.md`, `CodeCompass.md`, `A-project/index.md`, obsidian MCP (in `<root>/.mcp.json` or `ORBITER_MCP_SERVERS` names) → `{root, checks{…}, level: full|partial|none}`.
- Surfaced on `GET /api/health` as `workspace` — all sessions share `ORBITER_WORKSPACE_ROOT` today, so one probe serves all. `# ponytail:` no per-session cwd — trigger: sessions gain a spawn-cwd param, then move to `GET /api/sessions/{id}`.

### Services
- `_load_services()` in main.py (sibling of `_load_mcp_servers`): `ORBITER_SERVICES` = JSON `[{"name","url","embed"?}]` → added to `/api/health` as `services` (launcher URLs aren't secrets; no new endpoint).

## UI/UX wireframes (HUD aesthetic — no new fonts/colors; invoke `frontend-design` at implementation)

**Sidebar** — tab row replaces the `01 / SESSIONS` label (local `useState`, no router): `01 / [AGENTS] SKILLS SERVICES ↻`, active tab `text-signal` + underline. AGENTS = existing session cards byte-identical. SKILLS = rows: name + source badge (`PROJECT`/`USER`/`PLUGIN:x`, text-faint) + 2-line description. SERVICES = tiles: name + host, `↗` opens new tab; `EMBED` badge sets `store.embed`; below, an `MCP SERVERS` section reusing the HOST-panel row style.

**Console** — when `store.embed` set, transcript area swaps for `▸ SERVICE / <NAME> … ✕ CLOSE` header + full-height `<iframe>`; close restores transcript. `# ponytail:` plain iframe — trigger: first service needing an auth handshake.

**Telemetry** — `02 / SESSION` gains `SKILL ▸ <name>` row (or `—`). New `05 / PROJECT` panel (reuses `Panel`/`Readout`): `ROOT …jects/Orbiter`, `CEPHALON ● FULL/PARTIAL/NONE` (pip--go/hazard/crit) + four per-check ✓/✗ rows.

**Composer** — `MDL ▸ <model>` cycle button next to the `○/● TEAM` toggle (same 9px pattern; flat provider/model list is small). Default `MDL ▸ DEFAULT` → nothing sent.

## Data flow — switching provider/model

`init()` fetches `/api/providers` → Composer choice → `store.model` (persists across dispatches like `mode`) → POST body → `registry.resolve()` validates + assembles env (token from **server** env; client never sees it) → per-request `ClaudeSdkProvider(model=, env=)` → `ClaudeAgentOptions` → CLI subprocess inherits `ANTHROPIC_BASE_URL/AUTH_TOKEN` overrides. With one registry entry it's model-only today; the env seam ships dormant and tested.

## Adapter spec (→ `A-project/extending.md`, ~1 page)

- **Provider via registry:** append `{"name","base_url","auth_env","models"}` to `ORBITER_PROVIDERS`, export the token, restart — appears in the dropdown.
- **Provider via new Protocol impl:** implement `Provider.stream()` per the event contract (provider.py:42-61); `FakeProvider` is the reference; wire where `ClaudeSdkProvider` is constructed.
- **Service:** launcher → `ORBITER_SERVICES` entry (`embed:true` only for iframe-friendly targets); tool source → `ORBITER_MCP_SERVERS`, verify on `/api/health`.
- **Agent role:** `+ ROLES` panel or `POST /api/teams roles:[…]`; defaults in `orchestrator.DEFAULT_ROLES`.

## State model (types.ts / store.ts deltas)

```ts
interface SkillInfo    { name; description; source; path }
interface ServiceInfo  { name; url; embed? }
interface ProviderInfo { name; models: string[] }
interface Workspace    { root; level: "full"|"partial"|"none"; checks: Record<check, boolean> }
// Health gains: services: ServiceInfo[]; workspace: Workspace
// store: + skills, providers, model ({provider,model}|null), embed ({name,url}|null), setModel, setEmbed
// init() → Promise.allSettled([listSessions, getHealth, getSkills, getProviders])
// sessions/transcripts/approve*: UNCHANGED; sidebar tab = component-local state
```

## Tests (all SDK-free, existing pytest + TestClient patterns)

- `test_skills.py` — frontmatter variants; tmp-tree scan incl. fake `installed_plugins.json`; missing dirs → `[]`; endpoint via TestClient.
- `test_cephalon.py` — full/partial/none tmp roots; obsidian via `.mcp.json` vs external-MCP; `workspace` on `/api/health`.
- `test_registry.py` — load (valid/malformed/missing→default); **redaction proof** (`base_url`/`auth_env` absent from `public_view`); `resolve` incl. unknown→400 path.
- `test_providers_api.py` — `/api/providers` shape; POST with `model` → created session's provider is a `ClaudeSdkProvider` with `_model` set; no-model → global singleton.
- Services → extend `test_health_sandbox.py`. Frontend: tsc + build gate (no test framework — trigger: first tsc-invisible regression).
- Integration boundary (ADR, not test): one manual browser drive proving the real CLI honors `model=`/`env=` under z.ai — recorded like the delegate_many verification.

## Roadmap (each milestone = one commit, independently green)

| # | Milestone | Contents | Effort |
|---|-----------|----------|--------|
| M1 (DONE) | Sidebar tabs + SERVICES | `_load_services` + health; tab bar; tiles + MCP list; `embed` + ServiceFrame | ~0.5 day |
| M2 (DONE) | Skills | `skills.py` + `GET /api/skills`; SkillsList; active-skill readout; tests | ~0.5 day |
| M3 (DONE) | Cephalon probe | `cephalon.py` + health `workspace`; `05/PROJECT` panel; tests | ~0.25 day |
| M4 (DONE) | Provider registry + switcher | `registry.py`; `/api/providers`; POST params; SDK `model=`/`env=`; Composer dropdown; tests + manual z.ai drive → ADR | ~1 day |
| M5 (DONE) | Extension guide | `extending.md`; index.md update | ~0.25 day |

M1 first (M2's UI lands in the tab bar); M4 last among code (real-CLI risk); M5 documents what shipped.

**YAGNI cuts (triggers):** provider config file (>2 providers); tab router/deep-links (deep-link need); skill launch-from-UI (user asks); per-session cwd/multi-root probe (spawn-cwd param arrives); skills-scan cache (measured latency); iframe auth (first embedded login); frontend test framework (first tsc-invisible regression).

## Verification

Per milestone: `.venv/bin/pytest -q` · `cd web && npx tsc --noEmit && npm run build` · `ruff check <changed>`. End-to-end: `/run` skill (gateway + dashboard + browser smoke) — click each tab, open a service tile, check the Cephalon panel against the real workspace, dispatch a session with a non-default model and confirm it streams. Curl `/api/providers` and assert no `base_url`/`auth_env` in the payload.
