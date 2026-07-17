# Code Compass

> Reference from `CLAUDE.md`: "For context, read CodeCompass.md first, then CLAUDE.md"

**Agents: read the code for structure — this file holds only what the code cannot tell you.**
Do not add structure maps, entry-point lists, dependency tables, or folder-convention essays here — they drift the moment code changes and a model reads the actual code faster than it reads a stale map of it. Keep this ≤60 lines.

---

## What This Project Does

Railjack — a modular, per-machine **hub of local services**: one FastAPI backend
(`localhost:8700`) serving a React mission-control dashboard that embeds each
selected service's web UI (iframe) or a custom panel, and manages it — live
health, start/stop, ffmpeg jobs. Which modules ship is declared per machine in
`config/<machine>.yaml`; adding an iframe module is one YAML block, zero core
code. Formerly Orbiter (Agentic OS, retired 2026-07-18 — branch `legacy/agentic-os`).

## Current State

**Shipped (2026-07-18):** M0–M5 — rename + archive, hub shell + config loader +
tmux iframe, health fan-out + manage, ComfyUI live module, ffmpeg Video Lab
panel, hardening (pytest suite green) + docs. Full plan:
`A-project/plans/2026-07-18-railjack-hub-build.md`; overview `A-project/index.md`.
**Verify before commit:** `.venv/bin/pytest -q` · `cd web && npx tsc --noEmit &&
npm run build` · `.venv/bin/ruff check`.

## Hard Constraints

- **`comfy.sh` is the canonical ComfyUI manager** (`~/.claude/skills/f5-comfyui-media/scripts/comfy.sh`) — call it (start/stop/log); never reimplement the ComfyUI lifecycle in `app/`.
- **Never `shell=True`, never sudo.** ffmpeg op builders return argv **lists** run via `asyncio.create_subprocess_exec`.
- **Iframes hide, never unmount** — toggling visibility must not tear down the embeddable service (a tmux session served over ttyd dies if its iframe is unmounted; hide it instead).
- **One ffmpeg job at a time** — `app/ffmpeg_jobs.py` serializes via `asyncio.Lock` + a 409 if any job is non-terminal.
- **Every client-supplied path is confined** to a configured root (`media_dirs`/`lut_dir`/`output_dir`) via `os.path.commonpath` (`_safe_input`/`_safe_lut`).
- **`web/src/index.css` is the design system** — reuse verbatim, do not touch (vault recipe: `mission-control-ui-system`).
- **Port 8700, not 8000** (8000 was the retired Orbiter port).
- **Repo path contains a space** (`Coding Projects/Railjack`) — quote it in shells; in systemd units leave `WorkingDirectory` unquoted (values are literal).

## Known Gotchas

- uvicorn runs **without `--reload`** → restart after any route change (a stale server 404s new endpoints).
- Config is resolved at **import time** (`config.CONFIG = select_config()`); a missing/ambiguous config fails the uvicorn boot loudly, not at first request.
- n8n refuses framing until `frame-ancestors`/CSP is set on its side (see pivot ADR).

---

**Updated:** 2026-07-18
**Maintainer:** Naz
**Source:** Cephalon vault (`~/Cephalon/CodeCompass.md`) — copy to project root and customize
