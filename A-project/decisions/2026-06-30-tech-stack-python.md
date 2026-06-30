---
date: 2026-06-30
status: accepted
---

# ADR: Python over Rust for the Orbiter core

## Context

The agentic-OS blueprint (`../agentic-os-guide.md`) recommends a **Rust
microkernel** (ZeroClaw pattern) for the core runtime — single binary, memory
safety, trait-based abstraction. We are choosing **Python** instead.

## Decision

Implement the dashboard **and** the OS core in **Python**:
- Backend / core: FastAPI + `asyncio`
- Agent loop: `claude-agent-sdk` (v0.1.81)
- Frontend: React 19 + Vite + Tailwind v4 (unchanged from prior Phase 1)

## Rationale

- **`localhost` surface** — FastAPI/uvicorn serves the dashboard port directly;
  the user's stated requirement ("ability to localhost:XXXXX") is met by the
  same stack that runs the OS core.
- **`asyncio` maps cleanly to the HiveMind primitives** — semaphore admission
  control, rolling-window rate tracking, an AIMD controller, and a circuit
  breaker are all straightforward async state machines.
- **`claude-agent-sdk` is Python-native** — keeps the agent loop and the
  scheduler in one runtime.
- **Speed** — a fresh build should ship a working dashboard + Phase-1 primitives
  fast; Rust's bootstrap cost doesn't pay off until throughput/safety hardening.

## Consequences / deferred

- Rust microkernel stays open as a **future hardening** path if Python
  throughput or memory safety becomes the bottleneck.
- Single-language stack = simpler dev loop, at the cost of peak per-request
  performance (acceptable for a local single-user OS).
