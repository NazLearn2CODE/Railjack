# Railjack (né Orbiter) — Modular Local-Services Hub

> **Rename (Naz, 2026-07-18):** the project is renamed **Railjack**. Folder `~/Coding Projects/Orbiter` → `~/Coding Projects/Railjack`, package/app name `orbiter` → `railjack`, env var `ORBITER_CONFIG` → `RAILJACK_CONFIG`, output dir → `~/Videos/railjack-renders`, optional unit → `railjack.service`. The Orokin instance keeps its own (different) name — Naz will rename it separately; the Grimoldi config stub stays as the per-machine mechanism proof. All "Orbiter" references below read as Railjack; historical vault/ADR references to Orbiter stay accurate for the pre-pivot era.

## Context

Orbiter (`/home/NAZ/Coding Projects/Orbiter/` — path has a space, always quote) was an "Agentic OS" (FastAPI + React HUD, ~4.5k LOC). That purpose was retired 2026-07-09 when `agent-x` superseded bespoke orchestration (`30-decisions/2026-07-09-agent-x-supersedes-agentic-os.md`); the decision kept only its unique UI value. Naz is reviving Orbiter with a new vision: a **modular per-machine hub of selected localhost services** — home ("Tawhan" machine): tmux terminal (ttyd :7681) + ComfyUI (:8188) + an ffmpeg job form; office ("Orbiter Grimoldi" on Orokin, later): tmux + n8n.

**Decisions made with Naz:**
- Fresh build on `main` in the same repo; old code parked on an archive branch. Keep the mission-control HUD design language (`web/src/index.css` + `~/Cephalon/10-knowledge/mission-control-ui-system.md`), consult `/var/home/NAZ/open-design/` for layout.
- One codebase, per-machine module config. **No git remote yet** — home-first; Grimoldi sharing is a later phase.
- Hub depth = **embed + manage**: iframe/panel per module, plus live health pips and start/stop/restart controls.
- ffmpeg module = custom panel: operation dropdown (stitch/xfade/LUT/transcode), file picker, run, progress + logs.

**Ground truth (verified during planning — don't re-derive):**
- ttyd runs as systemd user units `ttyd.service` + `ttyd-upload.service`; sends **no X-Frame-Options** → iframe-safe.
- ComfyUI canonical manager: `/home/NAZ/.claude/skills/f5-comfyui-media/scripts/comfy.sh` (`status|start|stop|log`; health = GET `/system_stats`; start takes up to ~2 min). **Call it, don't reimplement.**
- ffmpeg recipes: `/home/NAZ/.claude/skills/f5-ffmpeg-video/references/recipes.md` (§M master flags, §0 normalize, §1 concat, §2 xfade offset formula, §3 lut3d, §8 DNxHR); preflight with `scripts/caps.sh`.
- Repo is dirty (modified AGENTS.md/CLAUDE.md, ~400 untracked `B-sessions/*.md`); branch `main`, no remote.
- Media dirs on this machine: `~/Downloads/B-Rolls`, `~/Videos/`, LUTs at `~/Videos/LUTs/` (has `warm-cinematic.cube`).

## M0 — Repo transition + rename

```bash
cd "/home/NAZ/Coding Projects"
mv Orbiter Railjack && cd Railjack   # rename first, so every later step uses the new path
git add -A && git commit -m "chore(legacy): final snapshot of the agentic-OS era before pivot to local-services hub"
git branch legacy/agentic-os && git tag legacy-agentic-os-final
# still on main — remove retired code (history stays on the branch/tag)
git rm -r app tests scripts web/src/components web/src/api.ts web/src/App.tsx web/src/store.ts web/src/types.ts web/src/util.ts
git rm -r B-sessions Z-harvest build
git rm A-project/agentic-os-guide.md A-project/api-reference.md A-project/architecture.md A-project/extending.md
```

Keep on main: `A-project/decisions/` (18 ADRs), `A-project/plans/`, `A-project/index.md` (rewrite), `assets/`, `web/src/index.css`, `web/index.html`, `web/vite.config.ts`, `web/package.json`, `web/tsconfig.json`, `.gitignore`, `README.md`/`CLAUDE.md`/`AGENTS.md`/`CodeCompass.md` (rewrite).

Same milestone: new ADR `A-project/decisions/2026-07-18-pivot-local-services-hub.md` (use `template.md`; record retirement→hub pivot **and the Orbiter→Railjack rename**, `git switch legacy/agentic-os` pointer, and the n8n frame-ancestors caveat for the office); rewrite `A-project/index.md`, `README.md`, `CLAUDE.md` under the Railjack name; rename `pyproject.toml` package to `railjack` (delete stale `orbiter.egg-info/`); add `B-sessions/` to `.gitignore`. Check for hardcoded old paths after the folder move: `grep -rn "Coding Projects/Orbiter" . --include="*.md" --include="*.py" --include="*.ts" -l` and fix hits (also check `.mcp.json` and any desktop launcher in `assets/`). Commit `chore(pivot): fresh start — Railjack, the local-services hub (formerly Orbiter)`.

**Run `/f5-vibe-check` before the `git rm` step** (non-coder owner; checkpoint exists via branch+tag).

**Verify:** `git log --oneline -3` shows the pivot; `git switch legacy/agentic-os && ls app/core` shows old files; back on `main`, no `app/`, `A-project/decisions/` has 19 files.

## Architecture

**Port 8700** (old app used 8000; avoid collisions). Deps: `fastapi`, `uvicorn[standard]`, `pyyaml`, `httpx` (remove `claude-agent-sdk`, `websockets` from `pyproject.toml`).

### Per-machine config — `config/<machine>.yaml`
Selection in `app/config.py`: `RAILJACK_CONFIG` env override, else match `hostname -s` (lowercase) against each config's `hostnames:` list; missing → clear startup error listing available configs. Files: `config/tawhan.yaml` (below; confirm real hostname first) + `config/orbiter-grimoldi.yaml` stub (tmux only, n8n commented out) to prove the mechanism.

```yaml
machine: tawhan
hostnames: [tawhan]          # output of `hostname -s`, lowercase — CONFIRM before writing
modules:
  - id: tmux
    title: TERMINAL
    kind: iframe
    url: http://localhost:7681
    health: { type: http, url: "http://localhost:7681/" }
    manage: { type: systemd-user, unit: ttyd.service, extra_units: [ttyd-upload.service] }
  - id: comfyui
    title: COMFYUI
    kind: iframe
    url: http://127.0.0.1:8188
    health: { type: http, url: "http://127.0.0.1:8188/system_stats" }
    manage:
      type: command
      start: ["bash", "/home/NAZ/.claude/skills/f5-comfyui-media/scripts/comfy.sh", "start"]
      stop:  ["bash", "/home/NAZ/.claude/skills/f5-comfyui-media/scripts/comfy.sh", "stop"]
      log:   ["bash", "/home/NAZ/.claude/skills/f5-comfyui-media/scripts/comfy.sh", "log"]
      start_timeout_s: 150
  - id: ffmpeg
    title: VIDEO LAB
    kind: panel
    panel: ffmpeg
    options:
      media_dirs: ["~/Downloads/B-Rolls", "~/Videos", "~/ttyd-drops"]
      lut_dir: "~/Videos/LUTs"
      output_dir: "~/Videos/railjack-renders"
```

Future module = one YAML block (`kind: iframe`) — zero core code. `kind: panel` additionally needs one React component + one entry in the frontend `PANELS` map.

### Backend (`app/`)
- `main.py` — loads config, routers, static-mounts `web/dist` **last** with placeholder-if-missing (copy pattern from legacy `app/main.py:459-473`).
- `config.py` — YAML load + pydantic models (Module, HealthSpec, ManageSpec).
- `health.py` — `GET /api/health`: parallel httpx checks, 3s timeout (copy `_one` + `gather(return_exceptions=True)` shape from legacy `app/core/registry.py:153-192`).
- `manage.py` — `POST /api/modules/{id}/action` (`start|stop|restart`): systemd-user → `systemctl --user <action> <unit>` (+extra_units); command → spec argv. `asyncio.create_subprocess_exec`, never `shell=True`, never sudo. Slow starts fire-and-forget → `{"status":"pending"}`, UI polls health. Surface stderr verbatim. `GET /api/modules/{id}/logs?n=50` → `journalctl --user -u <unit>` or the `log` command.
- `ffmpeg_jobs.py` — op builders returning argv (cite recipes.md section in comment): `transcode_h264` (§M), `concat` (§0+§1), `lut` (§3), `xfade` (§0+§2 offset formula), `transcode_dnxhr` (§8). Normalize (§0) built into ops, not user-facing. Path safety: every client path `expanduser().resolve()` + must be under a resolved `media_dirs`/`lut_dir` root (`os.path.commonpath`), else 400; outputs only to `output_dir` with timestamped names. In-memory jobs dict (status, progress %, `deque(maxlen=200)` log, output_path); one job at a time (`asyncio.Lock`); progress via `-progress pipe:1 -nostats` parsing `out_time_ms` ÷ total duration (ffprobe; subtract xfade overlaps). Endpoints: `GET /api/ffmpeg/files`, `GET /api/ffmpeg/luts`, `POST /api/ffmpeg/jobs`, `GET /api/ffmpeg/jobs[/{id}]`, `POST /api/ffmpeg/jobs/{id}/cancel` (SIGTERM). UI polls 1 s — no websockets.

### Frontend (`web/src/`)
- `index.css` — **keep verbatim, do not touch.**
- `main.tsx` (fonts from legacy `web/index.html`), `App.tsx` (`.field` root, TopBar, ModuleRail left, panel right), `api.ts` (fetch + `usePolling`), `store.ts` (zustand: config, healthMap, activeModuleId, jobs).
- Components: `TopBar` (machine name, clock, status pips), `ModuleRail` (`.hud--bracket` buttons + `.pip--go/--crit`/grey), `FramePanel` (adapt legacy `ServiceFrame.tsx`; **render all iframes once, toggle CSS visibility — never unmount**, or the tmux session reloads), `ManageBar` (health text, START/STOP/RESTART `.btn--signal`/`.btn--crit`, LOGS drawer), `FfmpegPanel` (op dropdown, ordered multi-select file checkboxes, op params — xfade duration, LUT picker, codec — RUN, job list with progress bars + log tail).
- `PANELS: Record<string, React.FC<{module}>> = { ffmpeg: FfmpegPanel }`.
- `vite.config.ts`: keep, proxy target → `http://127.0.0.1:8700`. `package.json`: drop `react-markdown`/`remark-gfm`.

Layout references (read during implementation): `/var/home/NAZ/open-design/design-templates/github-dashboard/` (rail + status-pip + panel composition) and `design-templates/flowai-live-dashboard-template/` (live polling/progress surfaces). Visual language stays Orbiter's `index.css`.

## Execution mode: delegation (Naz, 2026-07-18)

- **Host (this session) keeps:** M0 git surgery + rename (destructive — vibe-check gate), all architecture decisions, per-milestone verification (run the app, click through, read diffs), commits, and the vault updates.
- **Delegate via `agent-x`** (headless Claude Code child on the cheap provider, e.g. z.ai GLM — launched in the Railjack folder so it inherits the rewritten CLAUDE.md): the well-scoped build work of M1–M5, one milestone per child run, each brief containing the relevant plan section + reuse-verbatim pointers. Host reads the child's diff, runs the milestone's verify step, and only then commits. **Never merge unverified child output.**
- **Do NOT use local LLMs** this time — no `local-subagents`/`delegate-local`/Ollama (explicit instruction).
- If a child fails the same milestone twice, host takes it over directly (stop-digging rule) rather than looping.

## Milestones M1–M5 (commit after each; `feat(m1): …`)

- **M1 — Shell + config + tmux iframe.** `config.py`, minimal `main.py` (`/api/config` + static mount), both YAML files, frontend shell with all 3 rail entries (ffmpeg = "COMING SOON", ComfyUI frame may be dead). Setup: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`; `cd web && npm install && npm run build`. Run: `.venv/bin/uvicorn app.main:app --port 8700`.
  **Verify:** open `http://localhost:8700` — dark HUD, "TAWHAN" in top bar, 3 rail entries; TERMINAL shows live tmux (`ls` responds); switch to COMFYUI and back — terminal did **not** reload.
- **M2 — Health + manage.** `health.py`, `manage.py`, endpoints, pips (5 s poll), ManageBar.
  **Verify:** pips green (ComfyUI red/grey if down = expected). STOP on TERMINAL → pip red within ~5 s; START → green; `systemctl --user is-active ttyd.service` flips accordingly; LOGS shows journal lines.
- **M3 — ComfyUI live.** `command` manage type via `comfy.sh`, fire-and-forget start + amber "STARTING…" pip within `start_timeout_s`.
  **Verify:** START → amber pulse → green within ~1–2 min → ComfyUI graph UI loads in frame. While up: `curl -sI http://127.0.0.1:8188 | grep -iE "x-frame|frame-ancestors"` must print nothing. STOP frees VRAM.
- **M4 — ffmpeg Video Lab.** `ffmpeg_jobs.py` + endpoints + `FfmpegPanel`. Op order: `transcode_h264` → `concat` → `lut` → `xfade` → `transcode_dnxhr`. First run `bash ~/.claude/skills/f5-ffmpeg-video/scripts/caps.sh`.
  **Verify:** two short clips in `~/Downloads/B-Rolls` → xfade 0.5 s → progress to 100%, DONE, output under `~/Videos/railjack-renders/` plays with a dissolve; LUT op with `warm-cinematic.cube` looks warmer.
- **M5 — Hardening + docs.** Pytest: config selection (env override, hostname, missing-file), path-escape rejection (`../../etc/passwd` → 400), xfade offset math, job state machine (mock subprocess). Rewrite `CodeCompass.md`; update `A-project/index.md`. Optional: `railjack.service` user unit + desktop launcher (reuse `assets/` icons, relabeled).
  **Verify:** `.venv/bin/pytest` green; `npm run build` clean; full M1–M4 click-through.

## Risks / caveats

1. **n8n refuses framing by default** (office, later) — needs `N8N_CONTENT_SECURITY_POLICY`/frame-ancestors on the n8n side; recorded in the pivot ADR. Generic check for any module: `curl -sI <url> | grep -iE "x-frame-options|content-security-policy"`.
2. **Iframe unmount = lost terminal** — hide, don't unmount (most likely cheap-model regression; M1 verify catches it).
3. **`systemctl --user` needs the user session** — "Failed to connect to bus" means launched without `XDG_RUNTIME_DIR`; run from a normal terminal or a systemd *user* unit. Never sudo.
4. **ComfyUI start latency/VRAM** — don't loop restarts; pending state + `comfy.sh log` covers it.
5. **Paths** — repo path has a space (quote); YAML `~` paths need `expanduser().resolve()`; reject anything outside configured roots.
6. **One ffmpeg job at a time**; xfade re-encodes (slow is normal); §0 normalize is built into ops.

## Session-end vault updates

- Reframe + rename `~/Cephalon/20-projects/orbiter.md` → `20-projects/railjack.md` (new hub vision, status, rename note, pointer to `legacy/agentic-os`) — the 2026-07-09 decision explicitly asked for the reframe "when next touched". Update inbound `[[orbiter]]` links (`mission-control-ui-system.md`, `index.md` if present).
- Update the machine-local-resources table in `00-raw_ideas/machine-differences-naz-vs-producer.md` (`Coding Projects/Orbiter` → `Railjack`; note Orokin instance rename pending, name TBD by Naz).
- Update `hot.md` Orbiter line (🟡 reframed → 🟢 Railjack, active hub build), append one-line `logs/memory-log.md` entry, run `python3 vault-check.py`.

## Reuse-verbatim table

| What | Source | Into |
|---|---|---|
| Design system | `web/src/index.css` (untouched) | same path |
| Static mount + placeholder | legacy `app/main.py:459-473` | new `app/main.py` |
| Parallel health fanout | legacy `app/core/registry.py:153-192` | `app/health.py` |
| Iframe host structure | legacy `web/src/components/ServiceFrame.tsx` | `FramePanel.tsx` (+hide-don't-unmount) |
| Vite proxy config | `web/vite.config.ts` (8000→8700) | same path |
| Fonts/HTML shell | `web/index.html` | same path |
| ADR template | `A-project/decisions/template.md` | new ADR |

---

## M6 — Cockpit controls + session telemetry (approved by Naz, 2026-07-18)

Three refinements, all zero-context-cost by design: dropdowns/buttons only *type*
short strings into the terminal (type-only, NO Enter — Naz reviews and presses
Enter); telemetry is read from disk by the backend, never through the Claude session.

**Ground truth (verified during planning — don't re-derive):**
- ttyd runs `tmux new -A -s main`, writable (`-W`) → inject via
  `tmux send-keys -t main -l -- <text>` (`-l` literal, `--` guards leading `-`/`/`).
- Skills = dirs under `~/.claude/skills/` (21 today, `f5-*` family prefix).
- MCP servers = `mcpServers` keys in `~/.claude.json` (global) + per-project
  `projects.<path>.mcpServers` + optional `<project>/.mcp.json`.
- Session transcripts = `~/.claude/projects/<slug>/<uuid>.jsonl`. Last assistant
  entry's `message.usage` → context = `input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens` (live sample: ~90k). `message.model` identifies
  provider (`claude-*` → anthropic, `glm-*` → zai).
- 5h block math (ccusage convention): current block start = first message after
  a ≥5h gap, floored to the hour; `reset_at = start + 5h`.

### Backend
- `app/terminal_input.py` — `POST /api/terminal/insert {text}`: strip/refuse
  `\n`/`\r` (400) so nothing can auto-execute; max 500 chars; argv exec of
  `tmux send-keys -t <session> -l -- <text>`; tmux session name from the tmux
  module's `options.tmux_session` (default `main`). Surface stderr on failure.
- `app/catalog.py` — `GET /api/catalog`: skills (scan skills dir) + MCPs (parse
  the JSON sources above), each `{name, insert, group}`. Insert text: skills →
  `/name `, MCPs → template `use the {name} MCP to ` (template configurable).
  Grouping: ordered regex rules from config, first match wins, fallback `OTHER`.
  In-process cache 60 s.
- `app/session_stats.py` — `GET /api/session`: newest `*.jsonl` by mtime (idle
  if >10 min stale) → last usage + model → match against `providers:` config
  list (name, model_match regex, window_hours, context_limit) → scan
  matching-provider timestamps in files with mtime < window+1 h for block start
  → `{provider, model, context_tokens, context_limit, context_pct, reset_at,
  idle}`. 30 s in-process cache. Unknown model → provider `"?"`, still show ctx.
- `app/config.py` — optional machine-level keys: `providers`, `buttons`,
  `catalog` (groups/assign/mcp_insert_template). Defaults keep grimoldi stub valid.
- `config/tawhan.yaml` — add `tmux_session: main` under tmux module options;
  `providers:` anthropic + zai (both `window_hours: 5`, `context_limit: 200000`);
  `buttons:` BOOTSTRAP (canned prompt: read hot.md/index.md/Cephalon.md/CLAUDE.md
  + .claude agents, run vault-check.py, `git pull --ff-only` CephalonVoid sync,
  one-line status) and COMMIT & PUSH (canned prompt: session-end per Cephalon.md
  — hot.md + memory-log, vault-check, commit, push). Naz edits prompts in YAML,
  no code change.

### Frontend
- `TopBar` right side: SKILLS + MCP dropdowns (grouped, HUD-styled), the two
  buttons, telemetry strip `PROVIDER · CTX nn% (mini-bar) · RESET h:mm` —
  countdown ticks locally between 15 s polls; ctx bar amber >70 %, crit >90 %;
  `IDLE` state when no fresh session.
- `ManageBar`: remove the HEALTH label/text row (rail pips keep service health).
- Selecting a dropdown item or button → `POST /api/terminal/insert` → auto-switch
  active module to TERMINAL so Naz sees the typed text.

### Tests (extend suite)
Block math (gap >5h starts new block, floor-to-hour, reset_at), provider
regex match + unknown-model fallback, insert endpoint rejects newline/oversize,
catalog grouping first-match-wins. Mock filesystem/tmux — no real tmux in tests.

**Verify:** pytest green; live: dropdown pick → text appears in tmux pane
(`tmux capture-pane -p -t main | tail`), NOT executed; BOOTSTRAP types its
prompt; TopBar ctx% ≈ real session, RESET plausible vs block start; ManageBar
shows no HEALTH; 0 console errors headless.
