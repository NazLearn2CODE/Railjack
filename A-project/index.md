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
- **`main` is the working line** — `feat/gateway-dashboard` plus four follow-up branches merged in (all fast-forward). FastAPI gateway (REST + WS), React 19 dashboard, HiveMind scheduler primitives (AIMD / circuit-breaker / rate-limit / admission / token-budget), streaming agent loop, and **per-call tool approval via a PreToolUse hook** (Bash/Write/Edit gated, read-only auto-run) — see `[[2026-07-01-tool-approval-pretooluse-hook]]`.
- **Browser-verified end-to-end** under the z.ai/GLM backend: composer → WS token stream → completion, and approval card → APPROVE → gated Bash executes. ADR #8's "PreToolUse hooks fire under z.ai" upgraded from a code spike to a confirmed UI loop. Token telemetry **and** the per-session budget both count full throughput (input + cache + output); budget ceiling 200k.
- **Codebase audit-clean + type-safe:** ponytail audit collapsed the last duplication/dead code; `tsc --noEmit` is green and `npm run build` gates on it.
- **Security floor shipped (blueprint §2.2 L1/L2/L4):** `app/core/security.py` enforces a policy floor at the PreToolUse hook that **outranks operator approval** — catastrophic shell commands (`rm -rf /`, `mkfs`, `dd of=/dev/…`, fork bombs, pipe-to-shell, …) and out-of-workspace `Write`/`Edit` hard-deny *before* the approval card; every gated call writes one HMAC-SHA256 receipt to `logs/receipts.jsonl`. Env-configured (`ORBITER_WORKSPACE_ROOT` / `ORBITER_RECEIPT_LOG` / `ORBITER_RECEIPT_SECRET`). 26 tests green — see `[[2026-07-01-security-l1-l2-l4]]`.
- **Next stage — fill in the OS core** (per `[[architecture]]`: dashboard/gateway done, OS core next): (1) **L3 OS sandbox** — Landlock→Bubblewrap→Docker (the one remaining security layer; L1 workspace boundary, L2 shell policy, L4 HMAC receipts are done); (2) **protocol-based core** — `Provider` (LLM) + `Channel` (messaging) `Protocol`s so the core depends on abstractions, not the SDK; then (3) **orchestration** — MCP host/client/server + 2DOT topologies (guide Phase 2). Native Anthropic-API verification is a lower-priority cross-check (approval gate proven on z.ai only).
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
