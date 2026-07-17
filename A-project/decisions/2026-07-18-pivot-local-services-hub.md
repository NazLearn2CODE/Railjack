---
title: Railjack - Decision
date: 2026-07-18
topic: pivot-local-services-hub
status: accepted
---

# 2026-07-18 - Pivot: Orbiter (Agentic OS) → Railjack (local-services hub)

## Context

Orbiter existed to orchestrate cheap models doing real work in Naz's folders.
On 2026-07-09 the `agent-x` skill (+ `local-subagents` + the host model)
delivered that with almost no bespoke infrastructure — vault decision
`~/Cephalon/30-decisions/2026-07-09-agent-x-supersedes-agentic-os.md` retired
the "I must build the OS" framing and said the unique surviving value is the
dashboard/UI. On 2026-07-18 Naz revived the project with a new purpose.

## Decision

The project is now **Railjack**: a modular, per-machine **hub of selected
localhost services** — each module embeds a service's web UI (or a custom
panel) and manages it (health, start/stop) — served as one mission-control
dashboard on `localhost:8700`. The agentic-OS code is retired from `main`.

## Rationale

| Option | Pros | Cons | Decision |
|--------|------|------|----------|
| Strip old app in place | less rewriting | 4.5k LOC of retired concepts to operate around; risky for cheap-model implementation | ❌ |
| Brand-new repo | totally clean | loses history, 18 ADRs, design system continuity | ❌ |
| Fresh build on `main`, legacy archived on a branch | clean codebase + full history reachable | one-time git surgery | ✅ Chosen |

Also decided: **rename Orbiter → Railjack** (folder, package, env prefix).
The office instance on Orokin keeps a separate name (was to be "Orbiter
Grimoldi"; final name TBD by Naz — it is renamed separately on that machine).

## Impact

- Old code: branch `legacy/agentic-os` + tag `legacy-agentic-os-final`
  (full snapshot at `a9ff095`, including previously-untracked session logs).
  Retrieve with `git switch legacy/agentic-os`.
- Kept on `main`: `A-project/decisions/` (all prior ADRs), `A-project/plans/`,
  `web/src/index.css` (the mission-control design system — reused verbatim),
  `web/` build scaffolding, `assets/`.
- New architecture: lean FastAPI backend (port **8700**), per-machine module
  config in `config/<machine>.yaml` (selected by hostname or `RAILJACK_CONFIG`),
  React frontend with an iframe/panel module system.
- Phase-1 modules (home): tmux terminal (ttyd :7681), ComfyUI (:8188 via the
  f5-comfyui-media `comfy.sh`), ffmpeg "Video Lab" job panel (recipes from
  f5-ffmpeg-video).
- `B-sessions/` is now gitignored (machine-local runtime logs; the old
  "restore --staged before commit" rule is obsolete).
- **Office carry-over caveat:** n8n refuses iframe embedding by default —
  its module needs frame-ancestors/`N8N_CONTENT_SECURITY_POLICY` configured
  on the n8n side. Generic preflight for any module:
  `curl -sI <url> | grep -iE "x-frame-options|content-security-policy"`.

## Reversible?

Yes — the entire pre-pivot tree is one `git switch legacy/agentic-os` away,
and the tag `legacy-agentic-os-final` pins it permanently.

---
*For Claude: This captures project decisions with context. Reversible decisions can be changed later.*
