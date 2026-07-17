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
- **Office (Orokin)**: separate instance, separate name (TBD by Naz); future
  modules tmux + n8n. n8n needs frame-ancestors config before it can be
  embedded — see the pivot ADR.

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
| M2 | Health fan-out + manage (start/stop/logs) | ⚪ pending |
| M3 | ComfyUI module live | ⚪ pending |
| M4 | ffmpeg Video Lab panel | ⚪ pending |
| M5 | Hardening + docs | ⚪ pending |

## Working conventions

- Commit directly to `main`, one coherent increment per commit.
- Verify before commit: `.venv/bin/pytest -q` · `cd web && npx tsc --noEmit &&
  npm run build` · `.venv/bin/ruff check`.
- Safety: f5-vibe-check before/after non-trivial changes; f5-stop-digging after
  a 3rd failed fix.
