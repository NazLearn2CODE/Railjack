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
- `feat/gateway-dashboard` **merged to `main`** (fast-forward). FastAPI gateway (REST + WS), React 19 dashboard, HiveMind scheduler primitives (AIMD / circuit-breaker / rate-limit / admission / token-budget), streaming agent loop, and **per-call tool approval via a PreToolUse hook** (Bash/Write/Edit gated, read-only auto-run) — see `[[2026-07-01-tool-approval-pretooluse-hook]]`.
- **Browser smoke-tested end-to-end** (`/tmp/orbiter-smoke`): composer → WS token stream → completion, and approval card → APPROVE → gated Bash executes. The approval gate is **browser-verified to fire under z.ai**, upgrading ADR #8 from a code-spike claim to a confirmed UI loop. Token telemetry **and** the per-session budget both count full throughput (input + cache + output); budget ceiling raised to 200k.
- Next up (optional): real-backend verification on the native Anthropic API; the L1–L4 sandboxing layers (still TODO per the ADR).
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
