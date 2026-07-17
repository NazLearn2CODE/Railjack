# Railjack

A modular, per-machine **hub of local services** behind one mission-control
dashboard on `localhost:8700`. Each module embeds a service's web UI (tmux
terminal, ComfyUI, …) or a custom panel (ffmpeg Video Lab) and manages it —
live health pips, start/stop — from a single dark-HUD surface.

Formerly **Orbiter**, a locally-hosted Agentic OS; that purpose was retired
2026-07-09 (superseded by the `agent-x` skill). The old code lives on branch
`legacy/agentic-os` (tag `legacy-agentic-os-final`). Pivot rationale:
`A-project/decisions/2026-07-18-pivot-local-services-hub.md`.

## Stack

- **Python** — FastAPI + httpx + PyYAML; module registry, health fan-out,
  service management (`systemctl --user` / commands), ffmpeg job runner
- **React 19** + Vite + Tailwind v4 + Zustand — dashboard, reusing the
  mission-control design system in `web/src/index.css` verbatim

## Per-machine modules

Modules are declared in `config/<machine>.yaml`, selected by hostname
(`hostnames:` list) or the `RAILJACK_CONFIG` env override. This machine
("Tawhan", hostname `bazzite`): tmux terminal (ttyd :7681) · ComfyUI (:8188) ·
ffmpeg Video Lab. Adding an iframe module = one YAML block, zero core code.

## Run

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
.venv/bin/uvicorn app.main:app --port 8700
# open http://localhost:8700
```

## Vault

This repo is also an Obsidian project vault (`A-project/` docs + decisions;
`B-sessions/` machine-local session logs, gitignored). Project rules in
`CLAUDE.md`; start at `A-project/index.md`.
