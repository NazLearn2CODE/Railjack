---
title: PM Module Research Brief — Project Manager for Railjack
date: 2026-09-15
author: Tawhan (dsh :3080 research subagent, omarchy)
status: decisions-locked 2026-09-16 / BUILD DISPATCHED (agy ADW, H host)
sources: local file inventory (Cephalon, GymShelf, Railjack) + web research (free-first Jina/DDG)
---

# BRIEF — Railjack "Project Manager" module (PROJECTS tab)

Research + design synthesis for a module that reads a project folder, parses its
Cephalon-protocol markdown, infers status (phase / progress / health / blockers),
and shows **status + where-to-go-next** visually in Railjack's web UI. Targets:
large code projects, especially games.

---

## TL;DR

1. The Cephalon protocol is already a **parseable status database**: `A-project/index.md` carries an explicit "Current status" block (Working on / Next up / Last deployed); Railjack's own copy adds a milestone table with ✅ + dates + commit refs.
2. **`CodeCompass.md` is the highest-value status file by design** (Shipped / In progress / Blocked sections) — but GymShelf proves it ships **unfilled** (template verbatim). The parser must degrade gracefully and *report emptiness as a signal*, not crash.
3. Real status signals live in five places: index status block, milestone/checklist checkboxes, decision dates (`status:` frontmatter), `B-sessions/` "Next Steps" lines, and git history. Verified against GymShelf (small, healthy) and Railjack (large, milestone-driven).
4. `gates.yml` gives a **machine-checkable definition of "green"** — GymShelf has one (install/test/build); Railjack itself doesn't (its gates live in `A-project/index.md` Working conventions). Presence/absence of gates.yml is itself a maturity signal.
5. ADW artifacts (`*-BRIEF.md`, `*-CHECKPOINT.md`) carry explicit `Status:` lines (GymShelf: "SHIPPED 2026-09-14 · validator PASS") — a direct phase feed.
6. PM research says: use **RAG with pre-agreed mechanical thresholds** (never gut-feel), report **trend** alongside state, and give **dimension-level** color (schedule/quality/risk), not one blur.
7. Game-industry phase model is well-established and fits Naz's game targets: Concept → Pre-production (prototype/vertical slice) → Production → Alpha (feature-complete) → Beta (content-complete) → Gold → Live Ops. ADW (brief→build→gates→validate→shipped) is the tool-project equivalent.
8. "Next action" logic: prefer **explicit** next-steps the project itself wrote (B-sessions "Next:", index "Next up"), then blocker resolution, then first non-✅ milestone — don't invent tasks.
9. Progress % prior art: GitLab computes milestone % = closed items ÷ total items. For Naz: only count **explicit checkable units**; where none exist, show a coarse band (or nothing) — an honest "no estimate" beats a fake 63%.
10. Visual: a per-project **card grid** (RAG pip + phase bar + activity) with a **status header + next-action card** for the focused project. Railjack already has the HUD vocabulary: `.pip--go / .pip--hazard / .pip--crit / .pip--signal` pips, ModuleRail tabs, dark HUD.
11. Integration is the standard 3-touch module pattern: YAML block → `ProjectsPanel.tsx` in the PANELS map → `app/projects.py` APIRouter with `/api/projects/*`, included before the static catch-all. The **calendar module is direct precedent** for parsing vault markdown server-side.
12. v1 needs **zero LLM calls** — deterministic regex/YAML/git parsing only; an optional LLM "coach" summary can come later via `app/zai.py`.

---

## 1. Cephalon protocol file inventory (read + verified)

Template source: `~/Cephalon/90-templates/project-vault/` (+ `~/Cephalon/CodeCompass.md`,
`~/Cephalon/90-templates/initial-checklist.md`). Real instances checked: GymShelf
(`~/Coding Projects/GymShelf`) and Railjack (`~/Coding Projects/Railjack`).

| File (exact path per project) | What it contains | Status signals extractable |
|---|---|---|
| `A-project/index.md` | Purpose, tech stack, doc map, **Current status** block: *Working on / Next up / Last deployed* | **Direct**: current work, next action, deploy recency. Railjack's variant adds a **Milestones table** (`M0..M6.6`, ✅ + date + commit) → phase + completion % = ✅count÷total. Frontmatter `updated:` = doc freshness |
| `A-project/architecture.md` | Component map, data flow, tech-choice table | Near-zero status. Staleness only (does component map match reality?) |
| `A-project/api-reference.md` | Endpoint/schema tables | Near-zero status |
| `A-project/decisions/YYYY-MM-DD-topic.md` | ADRs: Context/Decision/Rationale with option table (✅/❌), frontmatter `status: proposed\|accepted\|deprecated` | **Decision dates = phase history timeline** (first decision ≈ project start; gaps = stalls). Latest topics ≈ current focus. `deprecated` = pivot. Decision density = momentum |
| `B-sessions/YYYY-MM-DD-*.md` | Session logs: Goals / What Was Done / **Issues Found** / **Next Steps** / Notes (+ `vault-assist:` lines) | **Direct**: next-action candidates ("Next: Naz review → shakedown → phase 2 Android"), blockers in prose ("No Android SDK on this box → Capacitor packaging (phase 2) blocked"). Newest file date = last activity |
| `CodeCompass.md` | ≤60-line session manual: **Current State (Shipped / In progress / Blocked)**, Hard Constraints, Env & Secrets, Deploy & Rollback, Tech-debt table | **The designated status file.** Filled ⇒ direct Shipped/InProgress/Blocked. ⚠️ GymShelf's copy is still the raw template — parser must detect "template, never filled" and flag it as a hygiene signal |
| `CLAUDE.md` / `AGENTS.md` | Project-agent rules, bootstrap order, vault read-only rules | Presence = project is agent-ready (bootstrap done). No status content |
| `initial-checklist.md` (copied from `90-templates/`, deleted when done) | ~25 checkboxes in 6 groups: Obsidian Protocol / Memory System / Planning / LLM Capabilities / Technical Setup / Cross-Machine | **Bootstrap completeness %** = checked÷total (per group too). File present + boxes unchecked = still in bootstrap phase; file absent = bootstrap finished |
| `gates.yml` | Named gate commands. GymShelf: `install`, `test`, `typecheck-build` (each a `mise x -- pnpm …` cmd) | **Executable definition of "green"** — run them (or read last-run results) ⇒ health. Absence = maturity gap (Railjack has none; its verify commands live in `A-project/index.md` § Working conventions: pytest · tsc+build · ruff) |
| `Z-harvest/harvest-report-template.md` (+ any filled `*.md`) | End-of-project harvest (lessons, techniques, decisions, debt) | **Filled harvest report present ⇒ project ended/archived.** Only-template = project not closed |
| `*-BRIEF.md`, `*-ADW-BRIEF.md`, `*-CHECKPOINT.md`, `*-PROGRESS.md` (project root) | ADW-cycle artifacts with an explicit **`Status:` line** — GymShelf-BRIEF.md: *"Status: SHIPPED 2026-09-14 (build `d218e9a` · host gates green · live HTTP 200 · validator PASS)"* | **Direct phase + verification state**: BRIEF→BUILD→GATES→VALIDATE→SHIPPED; checkpoint files = mid-build snapshots |
| `README.md` | Overview, install, stack | Low signal; stack hints for the tech column |
| `KANBAN.md` (Railjack-specific so far) | Native-kanban module **design spec** | Spec'd-but-unbuilt pattern: spec present, no matching code (`KanbanPanel.tsx`, `app/kanban.py`, PANELS entry all absent) ⇒ **planned-work signal** |
| git history (`.git`) | Conventional commits, dates, branch state | **Activity**: last-commit age, commits/14d sparkline; **phase progression** (GymShelf: `dcf0d4a` skeleton → `6c5c170` brief+decisions → `d218e9a` v1 → `1567e51` Android → `ed09ab1` playlists); dirty worktree = live WIP; `fix:`/`chore:` mix ≈ polish/maintenance phase |
| frontmatter (`machine:`, `owner:`, `scope:`, `updated:`) | YAML headers on protocol files | Ownership/machine binding (multi-machine drift guard) + doc recency |

Non-protocol-but-useful: `package.json`/`pyproject.toml` (stack, test cmd), `dist/` presence (built at least once), `.venv`/`node_modules` (initialized).

---

## 2. Status-inference mapping table

Proposed phase model (merges ADW with the game-industry pipeline — research §3.3):

`bootstrap → brief/planning → build (vN) → harden/gates → validated/shipped → live-ops/maintenance`

| Repo/markdown signal | Inferred phase | Progress % | Health (RAG) | Blocker? |
|---|---|---|---|---|
| `initial-checklist.md` present, boxes unchecked | bootstrap | checked÷total (explicit) | grey (not started) if 0% | — |
| Only `*-BRIEF.md`, status line `approved`/none, no `feat:` commits after brief | brief/planning | 5–15% band (no explicit units) | grey | — |
| Git: recent `feat:` commits; index "Working on" non-empty; CodeCompass "In progress" filled | build | ✅milestones÷total if milestone table exists, else activity-band | green if gates green + commit ≤14d | — |
| `gates.yml` exists; last known run green | (any) | — | **green** | — |
| Gate run red, or `Status:` line says validator FAIL | harden/gates | — | **red** until green | failing gate = the blocker (name the gate + cmd) |
| B-sessions "blocked"/"Issues Found" matching external-dep pattern ("needs X — not on this box", "waiting on Naz") | (any) | — | amber→red | **blocker text extracted verbatim** + source link |
| CodeCompass "Blocked:" line non-empty | (any) | — | red | direct |
| `*-BRIEF.md` `Status: SHIPPED <date> … validator PASS` | validated/shipped | milestone-based if table exists | green | next step usually "Naz review" (from session log) |
| Commits stop ≥14d while index says "Working on" | stalled | — | **amber** | "stale N days — resume or close out?" |
| Commits stop ≥30d, `fix:`/`chore:` tail, README says "shipped" | live-ops/maintenance | n/a | green/grey (dormant) | — |
| Filled report in `Z-harvest/` | archived | 100% | grey | — |
| CodeCompass.md = unfilled template | (any) | — | amber (hygiene) | "CodeCompass never filled — status is guesswork" (GymShelf's real state) |
| Milestone table with first non-✅ row | = current phase boundary | ✅÷total | — | next action = that milestone's scope |
| KANBAN.md-style spec without code | planned backlog | — | — | "spec'd, unbuilt" candidate next action |
| No A-project/, no protocol files at all | untracked project | — | grey | "no protocol — stamp via project-bootstrap" |

**RAG thresholds (mechanical, pre-agreed — the research's #1 rule):**
- **Green** = gates green (or no gates) ∧ commit ≤14d ∧ no blocker text.
- **Amber** = stale 14–30d ∨ status files unfilled/stale ∨ uncommitted changes ∨ next-step references an absent dependency.
- **Red** = explicit Blocked ∨ gate/validator FAIL ∨ stale >30d while claiming "in progress".
- Always show **trend** (activity sparkline / Δ commits vs prior 14d) next to state — research: direction of travel beats a bare color.
- Anti-"watermelon" rule: never render green while a gate run is red, regardless of prose.

**Next-action priority ladder** (first match wins; always cite the source file + quote):
1. Red blocker → its resolution ("Install Android SDK to unblock Capacitor packaging" ← B-sessions 2026-09-14).
2. Red gate → "run `gates.yml` / fix failing gate".
3. Newest session log "Next:" / index "Next up:" line.
4. Milestone table → first non-✅ milestone.
5. `initial-checklist.md` → first unchecked group.
6. Synthesized fallback: stale → "review & resume"; no protocol → "stamp project-bootstrap".

---

## 3. PM research findings (solo / small AI-accelerated teams, indie games)

### 3.1 RAG status — with mechanical thresholds
- Green ≠ perfect: it means *within agreed tolerances*; Amber is the most important and most-misused state (early warning that should trigger a documented response); Red = a problem the current authority/resources can't resolve — a call for a decision.
- The #1 failure is **subjective status**: thresholds must be agreed up front, stay stable, and status should be **per-dimension** (schedule, scope, quality, risk) — a single overall color hides more than it reveals.
- "Watermelon" projects (green outside, red inside) are the canonical failure of politeness-driven reporting; trend/direction-of-travel should ship alongside the current color.
- Sources: <https://www.instituteprojectmanagement.com/blog/rag-status-in-project-management/> · <https://www.clearpointstrategy.com/blog/establish-rag-statuses-for-kpis> · <https://eleco.com/pm3/knowledge-centre/how-many-rags/> · <https://portfoliohub.io/blog/rag-status>

### 3.2 Phase-gate for software, minus the ceremony
- Phase-gate (Stage-Gate®) = phases with a **gate review** at each boundary; value is *stage-limited commitment* — you decide go/kill/hold at the gate, not continuously. Origin: NASA/NPD practice; formalized in PMBOK.
- For a solo AI-accelerated shop the useful residue is small: **named phases + one explicit "gate check" per boundary** — which is literally what Naz's `gates.yml` + ADW validator already implement. No extra process needed; the module just *visualizes* the gates that exist.
- Sources: <https://www.smartsheet.com/phase-gate-process> · <https://umbrex.com/resources/frameworks/project-management-frameworks/stage-gate-phase-gate/>

### 3.3 Indie/game production phases and milestones
- Standard pipeline: **Concept → Pre-production (prototypes, vertical slice) → Production → Alpha (feature-complete) → Beta (content-complete, bug-fix only) → Release Candidate → Gold master → Live Ops**; pre-production is chaotic by design and progresses via successive real prototypes.
- **Vertical slice** = one representative chunk at shipping quality bar proving fun + pipeline (indie value debated vs AAA, but it maps cleanly to Naz's "v1 whole-app" briefs).
- Publisher milestone practice: **3–20 milestones per project** (first playable, alpha, beta…), each a short functionality description; *no industry standard* — per-project lists are normal. Railjack's `A-project/index.md` milestone table is exactly this, already in the protocol.
- Sources: <https://www.gamedevfoundry.com/StagesOfGameDevelopment.html> · <https://en.wikipedia.org/wiki/Video_game_development> · <https://www.reddit.com/r/gamedev/comments/5642qs/moving_from_aaa_to_indie_dev_is_the_vertical/>

### 3.4 Progress % and "next action"
- Prior art: GitLab milestones show **% complete = closed work items ÷ total work items** plus burndown/burnup. Transferable rule: derive % only from **countable, explicit units** (milestone ✅ rows, checklist boxes) — never from commit counts or vibes.
- GTD's "next action": for each *project*, define the single concrete physical next step; vague projects stall precisely because the next action is undefined. A PM dashboard that always answers "what's next" with a **sourced** action (quoting the project's own files) implements this.
- Kanban remains the solo-dev default for task flow (WIP-limited columns); for portfolio-level "which project is dying" the RAG grid is the better lens — that's the split this module should keep (status ≠ task board).
- Sources: <https://docs.gitlab.com/ee/user/project/milestones/> · <https://gettingthingsdone.com/what-is-gtd/> · <https://ones.com/blog/9-free-project-management-tools-for-solo-devs-in-2026/>

---

## 4. Visual display recommendation (Railjack PROJECTS tab)

One panel module, two levels:

```
┌ PROJECTS ──────────────────────────────────────────────────────┐
│ [grid of project cards — every project under ~/Coding Projects]│
│ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐          │
│ │ ●GymShelf     │ │ ●Railjack     │ │ ○<next>       │  ● = RAG │
│ │ ▓▓▓▓▓▓░░ build│ │ ▓▓▓░░░░░ build│ │ ▓░░░░░░░ brief│  phase   │
│ │ ▂▃▅▂ commits  │ │ ▅▂▁▁ 12d idle │ │ …             │  trend   │
│ │ next: Naz rev.│ │ next: first ✗M│ │               │          │
│ └───────────────┘ └───────────────┘ └───────────────┘          │
│ ── click a card ──                                             │
│ STATUS HEADER: name · RAG pip · phase · % (band) · last commit │
│ PHASE BAR:     bootstrap─brief─[build]─gates─shipped─live-ops  │
│ NEXT-ACTION:   "Install Android SDK → unblocks Capacitor"      │
│                source: B-sessions/2026-09-14-adw-v1.md          │
│ DETAIL:        milestones ✅list · gates (name + last result)  │
│                recent decisions · session log tail · sparkline │
└────────────────────────────────────────────────────────────────┘
```

Justification (brief):
- **Per-project grid first** — Naz's actual question is portfolio-level ("what's neglected, what's blocked"); RAG pips per card give the seconds-to-absorb scan the RAG research is built for. Reuses Railjack's existing pip vocabulary (`.pip--go/.pip--hazard/.pip--crit/.pip--signal` — verified in `frontend/src/index.css`).
- **Phase bar** beats a Gantt for a one-person shop: game phases are linear-ish; a segmented bar shows *where* a project is and *what gate is next* with zero maintenance burden.
- **One next-action card, always visible** — GTD's core: the dashboard must answer "what do I do next" without reading anything; every suggestion carries its **source quote** so Naz can trust or override it.
- **Trend sparkline** next to status (research: direction of travel). **Milestones list** only in detail view (counts already known — don't clutter).
- No kanban inside this module: task flow belongs to the (spec'd, unbuilt) KANBAN module; this one is **read-only status**.

---

## 5. Integration notes for Railjack specifically

Standard 3-touch module pattern (per `docs/module-authoring-guide.md` — verified against `app/main.py`, `frontend/src/App.tsx`, `configs/tawhan.yaml`):

**1. Config** — append to `modules:` in `configs/tawhan.yaml`:
```yaml
  - id: projects
    title: PROJECTS
    kind: panel
    panel: projects          # must match the PANELS key character-for-character
    options:                 # server-side only; never forwarded to the browser
      roots: ["~/Coding Projects"]   # folders to scan for projects
      extra_projects: []             # optional explicit paths (hybrid discovery)
      git_log_days: 14               # activity window
      cache_ttl_s: 300               # rescan throttle; RESCAN button bypasses
```

**2. Backend** — new `app/projects.py`, `router = APIRouter()`, wired in `app/main.py`
with `app.include_router(projects_router)` **before** the catch-all static mount
(line 128, `app.mount("/", StaticFiles(...))` — anything after it never fires):
- `GET /api/projects/summary` → `[{id, name, path, phase, health, trend, pct, pct_basis, next_action{ text, source_file, quote }, blockers[], last_commit, protocol: {index, codecompass_state, gates, decisions_n, sessions_n}}…]`
- `GET /api/projects/{id}` → detail (milestone rows, gate names/commands, decisions timeline, session-log tail, git log).
- `POST /api/projects/rescan` → force re-parse (mirror `GET /api/<module>/probe` convention from `newsroom.py`).
Implementation notes:
- **Read-only fs** everywhere; parse with stdlib (`yaml`, `re` for `- [ ]`/`- [x]`, simple frontmatter split; `python-frontmatter` only if already available). No LLM in v1; later optional summary via `app/zai.py`'s `zai_message()` (OmniRoute gateway — never z.ai direct).
- Git activity via `subprocess` **argv lists** (`asyncio.create_subprocess_exec`, `git -C <path> log --since=… --pretty=…`) — the `newsroom.py` rule, never shell=True.
- Vault-style path resolution precedent: `app/calendar_tasks.py` reads `~/Cephalon/20-projects/working-calendar-data.md` with a `CEPHALON_VAULT_PATH` override — copy that pattern for `~/Cephalon/20-projects/` cross-referencing (optional).
- Missing/broken files must degrade to `"codecompass_state": "template-unfilled"` style flags — never 500 the panel.

**3. Frontend** — `frontend/src/components/ProjectsPanel.tsx`, default-export
`FC<{ module: ModuleConfig }>`, registered in `App.tsx` as `projects: ProjectsPanel`
(the silent-mismatch trap is documented — key must match YAML `panel:` exactly).
Reuse: `fetchJSON` from `../api` (verified), Tailwind v4 + HUD design system in
`frontend/src/index.css`, existing pip classes for RAG. ModuleRail picks up the tab
automatically from config.

**Tests** — `tests/test_projects.py` (pytest; per-module test file is the house
convention): feed inline fixture trees for every parser rule (filled/unfilled
CodeCompass, milestone table, gates.yml, blocked-prose extraction, RAG thresholds,
next-action ladder). Gate: `.venv/bin/pytest -q` · `cd frontend && npm run build` ·
`.venv/bin/ruff check` (Railjack's de-facto gates.yml from `A-project/index.md`).

**Verify-after (non-negotiable, from the authoring guide):** `npm run build` →
`curl -X POST localhost:8700/api/config/reload` → `curl -s localhost:8700/api/projects/summary | jq` →
open `http://localhost:8700/#projects` and eyeball the grid.

**What to reuse (already exists):** health-probe pattern (`app/health.py`), calendar's
vault-md parsing, `zai.py` gateway (later), pips + HUD css, config hot-reload,
ModuleRail auto-tabs. **Complementary, not dependent:** the KANBAN module is
spec'd-but-unbuilt (`KANBAN.md`); PROJECTS can ship without it.

---

## 6. Decisions for Naz (grill-magnet — pick one each)

1. **Project discovery:** (a) auto-scan `~/Coding Projects/` for protocol markers (`A-project/`, `gates.yml`) · (b) explicit YAML list only · (c) **hybrid: auto-scan + YAML overrides/exclusions (recommended)** — zero-maintenance onboarding, escape hatch for non-code folders.
2. **Progress % honesty:** (a) only explicit counts (milestones/checklists), show nothing otherwise · (b) **explicit counts, else a coarse band (0–25/25–50/50–75/75–99) clearly labeled estimate (recommended)** · (c) never show %, phase-only. Research backs (a)/(b); git-derived % is a lie.
3. **Phase model:** (a) ADW lifecycle everywhere (brief→build→gates→validate→shipped) · (b) game pipeline everywhere (concept→…→live-ops) · (c) **per-repo type: games get game phases, tools get ADW phases, inferred from markers (`gamedev`/engine deps vs `gates.yml`+briefs) with YAML override (recommended)** — targets are games, but most current repos are tools.
4. **Health (RAG) authority:** (a) purely mechanical thresholds · (b) mechanical + LLM-written narrative overlay via zai.py · (c) mechanical + a per-project human-override file (e.g. `A-project/status.yml`). **Recommend (a) for v1**, keep (c) as the override hook; (b) later garnish.
5. **Scan model:** (a) parse on every panel load (simple, slow with many repos) · (b) **server cache TTL (~5 min) + RESCAN button (recommended)** · (c) systemd timer pre-warming. For ≤10 projects, (b) is plenty.
6. **Write-back:** (a) **strictly read-only now (recommended)** · (b) write a `status.yml` snapshot into each repo (drift + noise risk) · (c) later, an opt-in "update index.md Current status" button per project. Vault rule is never-write; his code repos deserve the same default until proven.

---

## 7. Next actions (ordered)

1. Naz answers the 6 decisions above (2 min each, they're pre-hammered).
2. Stamp the module skeleton: YAML block + `projects` in PANELS + `app/projects.py` empty router + `tests/test_projects.py` — verify the tab renders empty (catches the key-mismatch trap on day 0).
3. Implement the markdown parsers (index status block, milestone table, checklist, decisions frontmatter, CodeCompass state incl. "unfilled-template" detection) — TDD against GymShelf + Railjack as golden fixtures.
4. Add git-activity collector (argv-list subprocess) + trend sparkline data.
5. Implement inference: RAG thresholds, phase mapping (per decision 3), next-action ladder.
6. Build `ProjectsPanel.tsx`: grid → status header → phase bar → next-action card → detail.
7. Run the full verify-after chain; dogfood with GymShelf (expect: shipped/green, "Naz review → shakedown → phase 2") and Railjack itself (expect: build/green, next = first non-✅ milestone).
8. Iterate: per-game phase overrides, then optional zai.py one-line "coach" summary per project.

## 8. Decision record — LOCKED 2026-09-16 (Naz, via grill-me)

| # | Decision | Locked value |
|---|---|---|
| 1 | Discovery | **Hybrid**: auto-scan for protocol markers (`A-project/`, `gates.yml`) + YAML `exclude`/`extra_projects` overrides |
| 2 | Progress % | **Explicit counts only** (milestone ✅ / checklist boxes); else coarse band 0–25/25–50/50–75/75–99 **labeled "est."**; never git-derived |
| 3 | Phase model | **Per repo type**: `~/GameDev/*` → game phases (concept→…→live-ops); everything else → ADW phases; inferred from markers (`A-project/art-bible.md` or engine deps ⇒ game), YAML override per project |
| 4 | RAG authority | **Mechanical thresholds only** in v1 (the §2 table); `A-project/status.yml` reserved as future override hook; NO LLM calls in v1 |
| 5 | Scan model | **Server cache TTL 300 s + RESCAN button** (`POST /api/projects/rescan` bypasses) |
| 6 | Write-back | **Strictly read-only** — the module never writes to scanned repos |
| 7 | Scan roots | **`~/GameDev` ONLY** (Naz override of the recommendation — tools NOT scanned in v1; adding `~/Coding Projects` later = one YAML line) |
| 8 | Build route | **ADW: dispatch to agy** (this run), host (H/dsh) re-verifies gates independently |

### Hard constraints (Naz + verified host facts)

- **All module code stays inside the Railjack repo** — observe the existing module anatomy:
  `app/projects.py` (APIRouter, included in `app/main.py` BEFORE the line-128 static catch-all),
  `frontend/src/components/ProjectsPanel.tsx` (registered in the `PANELS` map, `App.tsx` line 13 —
  key must equal the YAML `panel:` value character-for-character), `tests/test_projects.py`,
  module block appended to `configs/tawhan.yaml`. No code outside the repo.
- Scan root resolved: `options.roots: ["~/GameDev"]`.
- Golden fixtures on disk: `~/GameDev/payont-siam` (full game-vault protocol, has `gates.yml`,
  index.md uses "Current phase" prose — parser must handle BOTH that and the milestone-table shape)
  and `~/GameDev/protocol` (repo #0, NO A-project → must render as "no protocol/untracked", never 500).

### Gates (host-verified commands, baseline green 2026-09-16)

- `cd ~/Coding\ Projects/Railjack && .venv/bin/pytest -q` (new `tests/test_projects.py` must pass; whole suite must stay green)
- `cd ~/Coding\ Projects/Railjack && .venv/bin/ruff check app/projects.py tests/test_projects.py`
- `cd ~/Coding\ Projects/Railjack/frontend && PATH="$HOME/.local/share/mise/installs/node/26.8.1/bin:$PATH" npm run build`
- After: `curl -X POST localhost:8700/api/config/reload` → `curl -s localhost:8700/api/projects/summary` returns JSON (2 projects) → `http://localhost:8700/#projects` renders the grid.
