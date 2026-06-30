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
- Working on: fresh bootstrap (2026-06-30). Vault + blueprint + stack declared.
- Next up: (1) rebuild Phase 1 dashboard; (2) implement blueprint Phase 1 OS-core primitives.
- Last deployed: n/a

## Build Rules

- **UI / dashboard work → invoke the `frontend-design` skill** (plugin installed globally).
- OS-core scheduling primitives map onto `asyncio` (semaphores, AIMD controller, circuit breaker).
- Rust microkernel is the guide's ideal but is **deferred** — see `decisions/2026-06-30-tech-stack-python.md`.

## Links to External Docs

- Blueprint source (vault, read-only): `~/Cephalon/agentic-os-guide.md`
- claude-agent-sdk: https://github.com/anthropics/claude-agent-sdk-python

---
*For Claude: this file tells you what Orbiter is and where things live. Start here, then `architecture.md`.*
