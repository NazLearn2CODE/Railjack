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
- **`main` is the working line** — early feature branches (`feat/gateway-dashboard`, `feat/security-l3-landlock`) merged in fast-forward; later increments commit directly to `main` (head: `031bc2e` — actionable worker approvals). On main now: FastAPI gateway (REST + WS), React 19 dashboard, HiveMind scheduler primitives (AIMD / circuit-breaker / rate-limit / admission / token-budget), streaming agent loop, **per-call tool approval via a PreToolUse hook** (Bash/Write/Edit gated, read-only auto-run — `[[2026-07-01-tool-approval-pretooluse-hook]]`), a **security policy floor** (L1/L2/L4 — `[[2026-07-01-security-l1-l2-l4]]`), a **kernel write-confinement backstop** (L3 self-Landlock at startup — `[[2026-07-01-sandbox-l3-landlock]]`), the **`Provider` trait** (`[[2026-07-01-provider-protocol]]`), the **centralized 2DOT topology** (`[[2026-07-02-centralized-2dot-topology]]`) + its gateway/dashboard surface, **external MCP** (`[[2026-07-02-external-mcp]]`), and **worker streaming lanes** (`[[2026-07-02-worker-streaming-lanes]]`) with **actionable worker approvals** (`[[2026-07-02-actionable-worker-approvals]]`).
- **Browser-verified end-to-end** under the z.ai/GLM backend: composer → WS token stream → completion, and approval card → APPROVE → gated Bash executes. ADR #8's "PreToolUse hooks fire under z.ai" upgraded from a code spike to a confirmed UI loop. Token telemetry **and** the per-session budget both count full throughput (input + cache + output); budget ceiling 200k.
- **Codebase audit-clean + type-safe:** ponytail audit collapsed the last duplication/dead code; `tsc --noEmit` is green and `npm run build` gates on it.
- **Security floor shipped (blueprint §2.2 L1/L2/L4):** `app/core/security.py` enforces a policy floor at the PreToolUse hook that **outranks operator approval** — catastrophic shell commands (`rm -rf /`, `mkfs`, `dd of=/dev/…`, fork bombs, pipe-to-shell, …) and out-of-workspace `Write`/`Edit` hard-deny *before* the approval card; every gated call writes one HMAC-SHA256 receipt to `logs/receipts.jsonl`. Env-configured (`ORBITER_WORKSPACE_ROOT` / `ORBITER_RECEIPT_LOG` / `ORBITER_RECEIPT_SECRET`). 26 tests green — see `[[2026-07-01-security-l1-l2-l4]]`.
- **L3 OS sandbox shipped (blueprint §2.2 Layer 3):** `app/core/sandbox.py` self-Landlocks the Orbiter process at startup via raw syscalls (`ctypes`, no new dep) — WRITE-type accesses confined to a small allowlist (workspace root, `/tmp`, `~/.claude`, + `ORBITER_SANDBOX_EXTRA_ROOTS`); reads/exec stay open. Restrictions inherit to the claude-agent-sdk CLI + its native Bash subprocess. Fail-open if Landlock is unavailable; status reported on `GET /api/health`. ADR `[[2026-07-01-sandbox-l3-landlock]]`, plan `[[2026-07-01-l3-landlock-sandbox]]`. **Runtime caveat:** this dev box's kernel has *no active Landlock LSM* (`/sys/kernel/security/landlock` absent — the probe returns `EFAULT`, not the `EPERM` the ADR predicted; fail-open is the observed path either way, and the syscall mechanics were confirmed via a `getpid` positive control). The gated confinement self-check (`ORBITER_SANDBOX_LIVE=1`) must pass on a landlock-capable host before L3 is runtime-confirmed. 31 tests green (1 gated check skips by default).
- **Provider trait shipped (blueprint §2.1 microkernel):** `app/core/provider.py` defines the `Provider` `Protocol` — the OS core now depends on the LLM trait, not `claude-agent-sdk`. `ClaudeSdkProvider` absorbs all SDK coupling (query/options/hook/serialize); `FakeProvider` (`tests/fakes.py`) is the second impl and runs the agent loop SDK-/network-free. **Channel deferred** (single impl today — the WS gateway). ADR `[[2026-07-01-provider-protocol]]`, plan `[[2026-07-01-provider-extraction]]`. 36 tests green.
- **Centralized 2DOT topology shipped (blueprint §1.1 + §3.2):** `app/core/orchestrator.py` — `Team` (supervisor + hired `WorkerRole`s) and the `delegate(role, task)` OS primitive. A worker **is** an `AgentSession`, so every delegation flows through the shared HiveMind scheduler (admission/AIMD/circuit-breaker/rate-limit/budget) and the security approval gate + receipt ledger — workers are first-class OS processes, not SDK-internal subagents (which would bypass the core). The supervisor gets a `delegate` tool via the SDK's **in-process MCP server** (`create_sdk_mcp_server` + `@tool`; the `mcp` package is already vendored — no new deps); workers run on a plain provider with no `delegate`, so depth is capped at 1. `delegate` never raises (failure/over-budget → string the supervisor can act on). ADR `[[2026-07-02-centralized-2dot-topology]]`. 43 tests green.
- **Topology surfaced (blueprint Phase 2 → gateway/dashboard):** `POST /api/teams` (`app/main.py`) builds a `Team`, hires roles (default `researcher` + `coder`, or caller-supplied), spawns the supervisor, and registers it — so a supervised run is driven and observed through the *existing* `/ws/sessions/{id}` stream, `/approve` gate, and `GET /api/sessions/{id}` (delegation surfaces as `delegate` tool calls + results in the Console). `AgentSession.kind` ("single" | "supervisor") badges it; `AgentSessionManager.register()` adopts the externally-built supervisor. Dashboard composer gains a `○/● TEAM` mode toggle → `createTeam()`. Default supervisor system-prompt is generated from the hired roles (`default_supervisor_prompt`). **Deferred (YAGNI, w/ triggers):** an in-dashboard role editor; a dedicated multi-lane team view.
- **External MCP shipped (blueprint §3.2 host/client):** `ClaudeSdkProvider(mcp_servers=...)` merges operator-configured external servers (stdio/sse/http) into the SDK `mcp_servers` option *alongside* the in-process `orbiter` delegate server (supervisor gets both; workers/single sessions get external only). Configured globally via `ORBITER_MCP_SERVERS` (JSON `{name: spec}`) — applied to every session, surfaced as name+type on `GET /api/health` (never env/headers — may carry secrets). The `Provider` Protocol stays SDK-free (concrete-impl concern, like `delegate`). ADR `[[2026-07-02-external-mcp]]`. **Trust boundary:** external MCP tools are operator-installed → trusted, so they bypass the PreToolUse gate (which matches only Bash/Write/Edit) — same posture as the `delegate` tool.
- **Worker streaming lanes shipped (richer team observability):** a worker's inner activity now streams onto the **supervisor's** bus — `AgentSession.event_sink` forwards every worker event as a nested `worker_event` frame (`{role, worker_id, event}`) via `Team.delegate`, and `AgentSession.ingest()` persists it to the supervisor's `messages` so it streams live *and* replays on `GET /api/sessions/{id}`. The dashboard renders each delegation as an inline `worker_lane` sub-panel (◂ DELEGATED · role + status pip) right where the supervisor called `delegate`, reusing the existing `Message` renderer for the worker's text/thinking/tool-use/result. No new endpoint — the supervisor's single `/ws` stream carries everything. ADR `[[2026-07-02-worker-streaming-lanes]]`. **Honest gap:** worker `approval_needed` is forwarded but shown only as a muted `⏸ WORKER GATE` indicator — workers aren't registered with the manager, so their gates aren't independently actionable yet (the next increment). 56 tests green.
- **Actionable worker approvals shipped (closes the lanes ADR's honest gap):** a delegated worker's gated tool (the `coder` role's Write/Edit/Bash) used to block until `APPROVAL_TIMEOUT` (600s) → fail-closed, because workers weren't registered with the manager. Now first-class approvable with **no new endpoint**: `Team(register=...)` (mirrors the `delegate`-callback pattern, wired to `manager.register` in `main.py`) sets `kind="worker"` and registers each worker before `run()`; `AgentSessionManager.list_sessions()` excludes `kind == "worker"` (sidebar stays clean — workers are observed via the supervisor's lane) while the existing `POST /api/sessions/{id}/approve` + `/ws` + GET detail drive them unchanged. Frontend: the `worker_lane` renders a real **shared `ApprovalCard`** (extracted from `Console.tsx`, bound via `onResolve`) that POSTs to `/api/sessions/{workerId}/approve` via a new `approveWorker` store action — the muted `⏸ WORKER GATE` indicator is gone. `tests/fakes.py` gains `FakeGatedProvider` (drives one call through `on_tool_use`). ADR `[[2026-07-02-actionable-worker-approvals]]`. 57 tests green.
- **Next — follow-on queue:** a dashboard surface for `ORBITER_MCP_SERVERS` (env-only today) → dedicated multi-lane team view → in-dashboard role editor. *Deferred (YAGNI):* hierarchical/decentralized topologies; shared team token-budget pool; per-worker autonomy levels; **Channel** (second microkernel trait — single impl today, the WS gateway); native Anthropic-API verification (approval gate proven on z.ai only).
- Last deployed: n/a (dev only, `localhost:8000`).

## Build Rules

- **UI / dashboard work → invoke the `frontend-design` skill** (plugin installed globally).
- OS-core scheduling primitives map onto `asyncio` (semaphores, AIMD controller, circuit breaker).
- Rust microkernel is the guide's ideal but is **deferred** — see `decisions/2026-06-30-tech-stack-python.md`.

## Working conventions (read before touching code)

- **Ponytail mode is ACTIVE (full)** — shortest working diff wins; reuse over rewrite; stdlib/native first; mark deliberate shortcuts with `# ponytail:` / `// ponytail:`. Audit your own diff before every commit (correctness bugs, duplication, dead code). Understand the full flow first, *then* be lazy.
- **Verify before every commit** — all three must be green:
  - `.venv/bin/pytest -q` (backend; `tests/fakes.py:FakeProvider` runs the agent loop SDK-/network-free)
  - `cd web && npx tsc --noEmit && npm run build` (frontend; `build` gates on tsc; `dist/` is gitignored)
  - `.venv/bin/ruff check <changed files>` (lint)
- **Commit directly to `main`** (the working line), one coherent increment per commit, conventional-commit message ending with `Co-Authored-By: Claude <noreply@anthropic.com>`. Stage only the increment's files — leave the untracked `B-sessions/2026-07-0*-s-*.md` runtime logs alone (they're agent-run artifacts, not increments). Update this index's *Current status* + *Next* as you ship.
- **OS primitives are tested SDK-free** via `FakeProvider`; the *LLM-choosing-to-call-X* boundary stays a real-LLM integration note recorded in an ADR (see `decisions/`).
- **Match the existing aesthetic** — dark HUD/phosphor (`hud--bracket`, `signal`/`hazard`/`go`/`crit` tones, `pip`, `label`, mono). No new fonts/colors.
- **Vault is READ-ONLY** (`~/Cephalon`) — never write it. Project memory lives here: `B-sessions/` (logs), `A-project/decisions/` (ADRs).

## Links to External Docs

- Blueprint source (vault, read-only): `~/Cephalon/agentic-os-guide.md`
- claude-agent-sdk: https://github.com/anthropics/claude-agent-sdk-python

---
*For Claude: this file tells you what Orbiter is and where things live. Start here, then `architecture.md`.*
