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
  mission-control design system in `frontend/src/index.css` verbatim

## Per-machine modules

Modules are declared in `configs/<machine>.yaml`, selected at boot by hostname
(`hostnames:` list) or the `RAILJACK_CONFIG` env override. This machine
("Tawhan", hostname `bazzite`): TERMINAL (ttyd) · ComfyUI · VIDEO LAB · RESEARCH
(NotebookLM) · NEWSROOM · THAILAND NOW · N8N. An iframe module is one YAML
block, zero core code; a panel module adds a React component + optional API
router (see `docs/module-authoring-guide.md`).

## Install

```bash
git clone https://github.com/NazLearn2CODE/Railjack.git
cd Railjack
uv sync                                       # python deps from uv.lock → .venv
cd frontend && npm install && npm run build && cd ..
cp .env.example ~/.config/railjack/env        # then edit: fill in the keys you need
mkdir -p ~/.config/systemd/user
cp assets/railjack.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now railjack         # hub up on http://localhost:8700
```

Config selection: the hub picks `configs/<hostname>.yaml` via each file's
`hostnames:` list, or `RAILJACK_CONFIG=<stem>` to force one. A miss fails fast
at boot with the available config names.

Creds prerequisites (only for the features you'll use — the hub boots without
them, serving 200):
- **LLM features** (NEWSROOM REWRITE, THAILAND NOW publicize, ComfyUI expand,
  NotebookLM polish) — `OMNIROUTE_API_KEY` in the env file, or the gateway key
  in `~/.config/omniroute/.env` (`app/zai.py` reads it as fallback). z.ai is
  never a hard dependency; calls ride the OmniRoute free-first cascade.
- **THAILAND NOW** — Google token via `python3 app/tn_auth.py` once; Trello
  key+token + Brave key in the env file (see `.env.example`).
- **NEWSROOM SEND TO NL** — optional: `python3 ~/.claude/skills/newsroom/scripts/nl_auth.py`
  for a newsroom-owned Google token (else it reuses the google-workspace MCP
  creds, which already work).
- **NotebookLM** — `notebooklm login` once.

Hot-reload config after YAML edits (no restart):
`curl -X POST http://localhost:8700/api/config/reload`.

## Vault

This repo is also an Obsidian project vault (`A-project/` docs + decisions;
`B-sessions/` machine-local session logs, gitignored). Project rules in
`CLAUDE.md`; start at `A-project/index.md`.
