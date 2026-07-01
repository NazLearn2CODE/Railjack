---
title: Architecture Overview
created: 2026-06-30
project: Orbiter
---

# Orbiter — Architecture

Maps the agentic-OS blueprint (`[[agentic-os-guide]]`) onto a Python implementation.

## System Overview

```
┌─────────────────────────────────────────────────────────┐
│  React dashboard (Vite + Tailwind + Zustand)            │  ← frontend-design skill
│        ▲ WebSocket (token stream)  ▼ REST (commands)    │
└────────┼────────────────────────────────────────────────┘
         │
┌────────┴────────────────────────────────────────────────┐
│  FastAPI gateway (localhost:<port>)                     │
│  ├─ claude-agent-sdk sessions                           │
│  ├─ plugin panels (JSON manifests)                      │
│  └─ reverse-proxy router (Google iframe embeds)         │
└────────┬────────────────────────────────────────────────┘
         │
┌────────┴────────────────────────────────────────────────┐
│  OS core (asyncio)                                      │
│  ├─ Admission control   (asyncio.Semaphore)             │
│  ├─ Rate-limit tracking (RPM / TPM windows)             │
│  ├─ AIMD backpressure   (concurrency controller)        │
│  ├─ Circuit breaker     (closed/open/half-open)         │
│  └─ Token budgeting     (per-agent ceiling → checkpoint)│
└────────┬────────────────────────────────────────────────┘
         │
┌────────┴────────────────────────────────────────────────┐
│  Security layer (Linux)                                    │
│  L1 workspace boundary · L2 shell policy · L4 HMAC receipts  ← DONE, PreToolUse hook (app/core/security.py)
│  L3 Landlock self-sandbox (write-confinement at startup)      ← DONE (app/core/sandbox.py); bwrap/Docker fallback deferred
└─────────────────────────────────────────────────────────┘
```

## Blueprint → Python mapping

| Blueprint concept | Python implementation |
|---|---|
| 5 HiveMind primitives | `asyncio`: semaphore admission, rolling-window rate tracking, AIMD controller, circuit-breaker state machine, token-budget guard |
| Microkernel / trait abstraction | `typing.Protocol` for `Provider` (LLM) and `Channel` (messaging) — core depends on protocols, not concretes |
| MCP integration | FastAPI = MCP host; `@modelcontextprotocol/server-obsidian` pattern reused; tools exposed as MCP servers |
| 2DOT topologies | Centralized first (supervisor + workers); hierarchical/decentralized later |
| Tool receipts | `ToolReceiptLedger` (`app/core/security.py`) — HMAC-SHA256 over canonical JSON per gated call → `logs/receipts.jsonl` (L1/L2/L4 at the PreToolUse hook; L3 = self-Landlock at startup, `app/core/sandbox.py`) |
| Dashboard surface | FastAPI WebSocket gateway + React UI (use **frontend-design**) |

## Build phases (from the guide)

1. **Foundation** — 5 HiveMind primitives + 4-layer security + protocol-based core. *(first real coding phase)*
2. **Orchestration & integration** — topologies + MCP host/client/server.
3. **Interface & operations** — dashboard + REST/WebSocket gateway + MCP Apps.
4. **Production readiness** — ZACTION (zero-shot actions) + SOP engine + autonomy levels.

> Phase 1 of the *prior* Orbiter built the **dashboard** (guide Phase 3) first.
> This fresh build re-establishes the dashboard, then fills in the OS core (guide Phase 1).

## Technology Choices

| Technology | Why | Alternatives considered |
|---|---|---|
| Python + asyncio | Fastest path to a working dashboard + OS core; matches `claude-agent-sdk` | Rust microkernel (guide's ZeroClaw ideal — **deferred**; revisit if throughput/safety demands) |
| FastAPI | Native WebSocket + REST on one `localhost` port | Node/Express (rejected: splits stack from OS core) |
| React 19 + Vite + Tailwind v4 | Prior Phase 1 stack; rapid UI via frontend-design | — |

---
*For Claude: reference this when touching architecture. Update when structure changes.*
