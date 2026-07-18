---
title: Project Index
created: 2026-06-30
updated: 2026-07-18
project: Railjack
---

# Railjack — Project Index

## Purpose

A modular, per-machine **hub of local services**: one mission-control dashboard
on `localhost:8700` that embeds each selected service's web UI (or a custom
panel) and manages it — live health, start/stop. Modules are declared per
machine in `config/<machine>.yaml`; adding an iframe module is one YAML block,
zero core code.

**Formerly Orbiter (Agentic OS)** — retired 2026-07-18, full rationale in
`[[2026-07-18-pivot-local-services-hub]]`. Old code: branch `legacy/agentic-os`,
tag `legacy-agentic-os-final`.

## Quick Context for Claude

**Tech stack:**
- Backend: Python ≥3.11, FastAPI + httpx + PyYAML (no LLM SDKs)
- Frontend: React 19 + Vite + Tailwind v4 + Zustand; design system =
  `web/src/index.css` **verbatim** (recipe: vault `mission-control-ui-system`)
- Key deps: see `../pyproject.toml`

**Where to find what:**
- Project decisions: `[[decisions]]` (ADRs; pre-pivot ADRs describe the legacy era)
- Session logs: `../B-sessions/` (machine-local, gitignored since the pivot)
- Per-machine module config: `../config/`
- Legacy architecture/API docs: on branch `legacy/agentic-os` only

**Machines:**
- **Tawhan** (home, hostname `bazzite`): modules tmux terminal (ttyd :7681),
  ComfyUI (:8188 via f5-comfyui-media `comfy.sh`), ffmpeg Video Lab panel.
- **Office (Orokin) → "Somatic"**: separate instance. Name decided by Naz
  2026-07-18 (`Somatic`, supersedes the "Orbiter Grimoldi" placeholder);
  name-only record, rename deferred (plan later on Orokin). Future modules
  tmux + n8n. n8n needs frame-ancestors config before it can be embedded —
  see the pivot ADR.

## Milestones

> **Resume protocol:** the full approved build plan is
> `[[plans/2026-07-18-railjack-hub-build]]` — a fresh session (any machine,
> any context window) resumes by reading that plan + this table, then doing the
> first non-✅ milestone. Each milestone's commit updates this table in the
> same commit (`feat(mN): …`).

| # | Scope | Status |
|---|-------|--------|
| M0 | Rename Orbiter→Railjack, archive legacy, fresh main | ✅ 2026-07-18 (`028458e`) |
| M1 | Shell + config loader + tmux iframe | ✅ 2026-07-18 (GLM child via agent-x; host-verified: browser smoke PASS — TAWHAN bar, 3 rail entries, live tmux frame, no-remount proof, 0 console errors) |
| M2 | Health fan-out + manage (start/stop/logs) | ✅ 2026-07-18 (GLM child via agent-x; host-verified: API — health `{tmux:ok,comfyui:down}`, restart ok, logs ok, 400/404/400 edge paths; browser — green/red/grey pips correct, ManageBar renders, 0 console errors) |
| M3 | ComfyUI module live | ✅ 2026-07-18 (host-built directly — ~40 lines; live verify: START→`pending`→amber `pip--hazard` "STARTING…"→green `pip--go` in headless browser, iframe-safe (no x-frame headers), STOP frees VRAM, 0 console errors) |
| M4 | ffmpeg Video Lab panel | ✅ 2026-07-18 (GLM child via agent-x; host fixed a TS syntax error, aligned §M/§8 flags to recipes.md — child couldn't read it — and injected the missing `-progress pipe:1 -nostats`; host-verified live: xfade→done@100% (9.5095 s output, dissolve confirmed), LUT warmth confirmed numerically (R 159→173, B 126→118), path-escape→400, concurrent→409, mid-run progress 22→53%, SIGTERM cancel→cancelled, headless UI pass with 0 console errors) |
| M5 | Hardening + docs | ✅ 2026-07-18 (GLM child via agent-x — child's sandbox couldn't exec pytest, so host ran the whole gate; host fixed child's `railjack.service` ExecStart word-split bug (space in repo path → quoted the executable; `WorkingDirectory` stays unquoted); child finding locked by test: `_safe_lut` confines but doesn't validate `.cube` extension (UX gap, not a hole). Host-verified: 26 pytest green (config/paths/xfade/jobs), ruff clean, tsc+`npm run build` clean, `systemd-analyze verify` ok, full M1–M4 headless click-through PASS — pips, ManageBar, real transcode job →done@100%, iframe no-remount, 0 console errors) |
| M6.2 | Video Lab file browser + terminal/voice refinements | ✅ 2026-07-18 (host-built directly, Naz-requested. **(a)** Terminal pitch-black: the grey `#2b2b2b` was baked into `~/.config/ttyd/index.html`'s xterm theme + page CSS (overrode the `-t` flag) — replaced with `#000000`; headless screenshot red→black confirmed. **(b)** Voice-to-text: diagnosed as the typing backend, not the mic — `faster-whisper-dictation` recorded+transcribed but injected via X11 xdotool (xclip not even installed) on a KDE-Wayland session, so nothing typed. Fix: created + enabled a `ydotoold` user service (uinput via logind ACL, no root) so the tool's Wayland typer works; patched the typer's paste to Ctrl+Shift+V (was Ctrl+V, ignored by terminals). Daemon restarts clean on the evdev hotkey path; needs Naz's voice for final end-to-end. **(c)** Video Lab browser: new `/api/ffmpeg/panel`, `/api/ffmpeg/browse` (confined under `browse_root`=~), `files?dir=` (recursive, confined), per-job `output_dir` (default `~/Downloads/VDO Outputs`, auto-created); input confinement widened to media_dirs ∪ ~. Frontend: ADD FOOTAGE folder picker + removable footage chips (localStorage), OUTPUT folder picker, hash deep-linking (`#<module>`). Verified: 61 pytest (11 new in test_browse.py), ruff, tsc+build clean, live end-to-end transcode → done@100% into auto-created VDO Outputs, headless panel screenshot PASS) |
| M6 | Cockpit controls + session telemetry (skills/MCP dropdowns, Bootstrap + Commit&Push buttons, ctx% + 5h-reset TopBar strip) | ✅ 2026-07-18 (GLM child via agent-x; sandbox again blocked exec → host ran the gate; host hardened insert to reject ALL control chars (child only blocked `\n\r` — a crafted POST could've delivered Ctrl-C/ESC), host added `<project>/.mcp.json` merge (obsidian was missing) + de-hardcoded the test fixture. Verified: 44 pytest green, ruff, tsc+build, live: catalog 21 skills + 8 MCPs grouped, /api/session real (anthropic · fable-5 · 66% · reset 04:00Z = block 23:00Z+5h), insert types-but-never-executes proven via tmux capture-pane, rejection matrix newline/^C/ESC/tab/oversize→400, headless UI PASS — optgroups, buttons, telemetry strip, HEALTH gone from ManageBar, dropdown resets, 0 console errors; M6.1 same-day (Naz: telemetry inaccurate): recalibrated to official usage APIs — anthropic OAuth /usage endpoint (token read from ~/.claude/.credentials.json, never logged) + z.ai rate-ck quota endpoint; per-provider `usage_source`/`key_env` in YAML; JSONL heuristic demoted to marked-estimate fallback (~ prefix); SES/WK % added to strip; unit gets optional EnvironmentFile for ZAI_API_KEY; 50 pytest; live verify matched Naz's /usage ground truth within 1 min, headless strip render PASS) |
| M6.3 | Autostart install + browser-tab rename | ✅ 2026-07-18 (host-built directly, Naz-requested. **(a)** Autostart: installed `assets/railjack.service` as a systemd USER unit (`enable --now` → `active`+`enabled`, symlinked into `default.target.wants` so it survives reboot); replaced the ad-hoc `uvicorn &` a prior session left on :8700 (killed first to free the port); z.ai key wired via `EnvironmentFile=-~/.config/railjack/env` (chmod 600, mirrored from `~/.bashrc`); `.desktop` + `orbiter-dev` icon installed to `~/.local/share`. **(b)** Tab: `web/index.html` title `ORBITER · Agentic OS Console`→`RAILJACK BRIDGE`, `dist/` rebuilt, live curl confirmed served title; commit `6a601d1`. **(c)** Office instance name recorded → `Somatic` (name-only, rename deferred).) |

## Working conventions

- Commit directly to `main`, one coherent increment per commit.
- Verify before commit: `.venv/bin/pytest -q` · `cd web && npx tsc --noEmit &&
  npm run build` · `.venv/bin/ruff check`.
- Safety: f5-vibe-check before/after non-trivial changes; f5-stop-digging after
  a 3rd failed fix.
