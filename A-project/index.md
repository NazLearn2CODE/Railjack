---
title: Project Index
created: 2026-06-30
project: Orbiter
---

# Orbiter — Project Index

## Purpose

A locally-hosted **Agentic OS**: an orchestration layer that treats AI agents as
OS processes — with scheduling, sandboxing, and observability — exposed through a
web dashboard on `localhost:<port>`. Built from the blueprint in
`[[agentic-os-guide]]`.

## Quick Context for Claude

**Tech stack:**
- Language: **Python** (backend + OS core)
- Backend: FastAPI + `claude-agent-sdk` (v0.1.81) + WebSocket streaming
- Frontend: React 19 + Vite + Tailwind v4 + Zustand
- Core runtime: `asyncio` (scheduling primitives)
- Key deps: see `../pyproject.toml`

**Where to find what:**
- Blueprint (full): `[[agentic-os-guide]]`
- Architecture overview: `[[architecture]]`
- API reference: `[[api-reference]]`
- Project decisions: `[[decisions]]`
- Session logs: `[[../B-sessions]]`

**Current status:**
- Working on: gateway + dashboard + OS core **built and committed** on branch `feat/gateway-dashboard` (7 commits ahead of `main`). FastAPI gateway (REST + WS), React 19 dashboard, HiveMind scheduler primitives (AIMD / circuit-breaker / rate-limit / admission / token-budget), streaming agent loop, and **per-call tool approval via a PreToolUse hook** (Bash/Write/Edit gated, read-only auto-run) — see `[[2026-07-01-tool-approval-pretooluse-hook]]`.
- Next up (optional, not started): (1) merge `feat/gateway-dashboard` → `main`; (2) manual UI smoke test end-to-end through the browser (composer → streamed output → approval card → approve/deny).
- Last deployed: n/a (dev only, `localhost:8000`).

## Build Rules

- **UI / dashboard work → invoke the `frontend-design` skill** (plugin installed globally).
- OS-core scheduling primitives map onto `asyncio` (semaphores, AIMD controller, circuit breaker).
- Rust microkernel is the guide's ideal but is **deferred** — see `decisions/2026-06-30-tech-stack-python.md`.

## Links to External Docs

- Blueprint source (vault, read-only): `~/Cephalon/agentic-os-guide.md`
- claude-agent-sdk: https://github.com/anthropics/claude-agent-sdk-python

---
*For Claude: this file tells you what Orbiter is and where things live. Start here, then `architecture.md`.*
