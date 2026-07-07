# Code Compass

> Copy this file to your project root. Reference it from `CLAUDE.md`: `For context, read CodeCompass.md first, then CLAUDE.md`

**Agents: read the code for structure — this file holds only what the code cannot tell you.**
Do not add structure maps, entry-point lists, dependency tables, or folder-convention essays here — they drift the moment code changes and a model reads the actual code faster than it reads a stale map of it. Keep this ≤60 lines.

---

## What This Project Does

Orbiter — a locally-hosted **Agentic OS**: an orchestration layer treating AI agents as OS processes (scheduling, sandboxing, observability) on a Python/asyncio core, served through a React dashboard on `localhost:8000`. Built solo by Naz (owner) from the `agentic-os-guide` blueprint.

## Current State

**Shipped:** first live end-to-end run + human visual verification (2026-07-02, commit on `main`); mission-control surface + two verification gaps closed (2026-07-06, commit `9bf41b1`). 79 pytest green · ruff clean · `tsc --noEmit` + `npm run build` clean.
**In progress:** demand-driven increments (the program runs; no increment is *needed* to run it).
**Blocked:** L3 Landlock runtime-confirmation — needs a Landlock-capable host (this dev box has no active LSM).

## Hard Constraints

- LLM backend is **z.ai/GLM only** — native Anthropic is *off the roadmap*, not deferred-pending-a-key. The `Provider` seam makes it a pure env key-swap if that ever reverses.
- Sandbox is **FAIL-OPEN + observable** on this box (no Landlock LSM) — a local single-user OS must not refuse to run. Surfaced honestly on `/api/health`, never greenwashed.
- uvicorn runs **without `--reload`** → restart after any route change (a stale server 404s new endpoints).
- No secrets in tracked files (`.env`, `.mcp.json`, `.claude/settings.json` token are machine-local).

## Env & Secrets

- `.env` (gitignored) — provider API keys.
- `ORBITER_RECEIPT_SECRET` — HMAC key for `logs/receipts.jsonl` (the gated-tool audit trail).
- `ORBITER_WORKSPACE_ROOT` / `ORBITER_SANDBOX_EXTRA_ROOTS` / `ORBITER_RECEIPT_LOG` — sandbox + receipt paths.
- No `sk-ant-` key anywhere (intentional — see constraint above).

## Deploy & Rollback

```bash
# Run (dev only — localhost:8000)
uvicorn app.main:app --port 8000

# Rollback to a prior commit
git checkout <prev> &&  # restart uvicorn — no --reload
```

## Known Gotchas

- `~/Cephalon/10-knowledge/testing-gotchas.md` — **SDK-free tests ship green-but-broken** (the test-seam-above-the-client gap; `FakeProvider` never hits the real CLI).
- `~/Cephalon/10-knowledge/claude-agent-sdk-gotchas.md` — `--session-id` is **UUID-only**; `mcp__<server>__<tool>` namespacing required in `allowed_tools`.
- `~/Cephalon/10-knowledge/typescript-gotchas.md` — type-only import syntax.
- `B-sessions/` runtime logs **auto-stage into the git index** → `git restore --staged B-sessions/` before every commit.

## Technical Debt / Known Ceilings

| Shortcut | Why | Upgrade path |
|----------|-----|--------------|
| L3 Landlock fail-open | No active LSM on dev box | `ORBITER_SANDBOX_LIVE=1` self-check on a Landlock host |
| `Channel` trait deferred | Single impl (WS gateway) today | Add a 2nd messaging impl when needed |
| Real-LLM WS drive = the integration test | SDK-free tests can't reach CLI paths | Keep one live z.ai drive per topology change |
| Per-role `allowed_tools` deferred | YAGNI | Expose when role-editor lands |

---

**Updated:** 2026-07-07
**Maintainer:** Naz
**Source:** Cephalon vault (`~/Cephalon/CodeCompass.md`) — copy to project root and customize
