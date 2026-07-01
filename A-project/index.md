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
- **`main` is the working line** — `feat/gateway-dashboard` plus follow-up branches merged in fast-forward (latest: `feat/security-l1-l2-l4`). FastAPI gateway (REST + WS), React 19 dashboard, HiveMind scheduler primitives (AIMD / circuit-breaker / rate-limit / admission / token-budget), streaming agent loop, **per-call tool approval via a PreToolUse hook** (Bash/Write/Edit gated, read-only auto-run — `[[2026-07-01-tool-approval-pretooluse-hook]]`), and a **security policy floor** (L1/L2/L4 — `[[2026-07-01-security-l1-l2-l4]]`).
- **Browser-verified end-to-end** under the z.ai/GLM backend: composer → WS token stream → completion, and approval card → APPROVE → gated Bash executes. ADR #8's "PreToolUse hooks fire under z.ai" upgraded from a code spike to a confirmed UI loop. Token telemetry **and** the per-session budget both count full throughput (input + cache + output); budget ceiling 200k.
- **Codebase audit-clean + type-safe:** ponytail audit collapsed the last duplication/dead code; `tsc --noEmit` is green and `npm run build` gates on it.
- **Security floor shipped (blueprint §2.2 L1/L2/L4):** `app/core/security.py` enforces a policy floor at the PreToolUse hook that **outranks operator approval** — catastrophic shell commands (`rm -rf /`, `mkfs`, `dd of=/dev/…`, fork bombs, pipe-to-shell, …) and out-of-workspace `Write`/`Edit` hard-deny *before* the approval card; every gated call writes one HMAC-SHA256 receipt to `logs/receipts.jsonl`. Env-configured (`ORBITER_WORKSPACE_ROOT` / `ORBITER_RECEIPT_LOG` / `ORBITER_RECEIPT_SECRET`). 26 tests green — see `[[2026-07-01-security-l1-l2-l4]]`.
- **L3 OS sandbox shipped (blueprint §2.2 Layer 3):** `app/core/sandbox.py` self-Landlocks the Orbiter process at startup via raw syscalls (`ctypes`, no new dep) — WRITE-type accesses confined to a small allowlist (workspace root, `/tmp`, `~/.claude`, + `ORBITER_SANDBOX_EXTRA_ROOTS`); reads/exec stay open. Restrictions inherit to the claude-agent-sdk CLI + its native Bash subprocess. Fail-open if Landlock is unavailable; status reported on `GET /api/health`. ADR `[[2026-07-01-sandbox-l3-landlock]]`, plan `[[2026-07-01-l3-landlock-sandbox]]`. **Runtime caveat:** this dev box's kernel has *no active Landlock LSM* (`/sys/kernel/security/landlock` absent — the probe returns `EFAULT`, not the `EPERM` the ADR predicted; fail-open is the observed path either way, and the syscall mechanics were confirmed via a `getpid` positive control). The gated confinement self-check (`ORBITER_SANDBOX_LIVE=1`) must pass on a landlock-capable host before L3 is runtime-confirmed. 31 tests green (1 gated check skips by default).
- **Next stage — fill in the OS core** (per `[[architecture]]`: dashboard/gateway + full 4-layer security done, OS core next): (1) **protocol-based core** — `Provider` (LLM) + `Channel` (messaging) `Protocol`s so the core depends on abstractions, not the SDK; then (2) **orchestration** — MCP host/client/server + 2DOT topologies (guide Phase 2). Native Anthropic-API verification is a lower-priority cross-check (approval gate proven on z.ai only).
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
