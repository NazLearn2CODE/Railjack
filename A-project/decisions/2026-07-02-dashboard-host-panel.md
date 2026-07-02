---
date: 2026-07-02
status: accepted
---
# ADR: Dashboard host-posture panel (blueprint Phase 2 — observability)

## Context

External MCP landed env-only (`[[2026-07-02-external-mcp]]`) and the L3 sandbox
shipped fail-open on this box (`[[2026-07-01-sandbox-l3-landlock]]`), but neither
was visible in the dashboard — the operator couldn't see *which* MCP servers an
agent would reach, nor whether the sandbox was actually confining writes. The
follow-on queue's top item was "a dashboard surface for `ORBITER_MCP_SERVERS`".
`GET /api/health` already exposed both (MCP as name+type only, never secrets;
sandbox as active/mechanism/reason), but the frontend never consumed it.

## Finding

The honest lazy change is **render, not build**: the data already exists, the
`Telemetry` column already has reusable `Panel`/`Readout` primitives and an open
readout slot. A read-only `04 / HOST` panel reuses them verbatim — zero new
component files, zero backend change. This is observability only; the external-MCP
ADR already deferred a *config* UI as YAGNI (env-only, local single-user OS), and
this doesn't reopen that.

## Decision

- **`web/src/api.ts`** — `getHealth()` fetches `/api/health`.
- **`web/src/types.ts`** — `Health`/`SandboxStatus`/`McpServer` mirror the backend
  shape (`abi: string | null` — null when landlock is unavailable, confirmed by curl).
- **`web/src/store.ts`** — `health: Health | null`; `init()` fetches it
  **independently** of `listSessions()` so a flaky health check never breaks the
  sidebar (both swallow errors).
- **`web/src/components/Telemetry.tsx`** — a `04 / HOST` panel: a `SANDBOX`
  `Readout` (`<MECHANISM>` when active, `FAIL-OPEN` muted when not) + an `MCP`
  `Readout` (count or `NONE LOADED`) + one mono row per loaded server
  (`▸ name · type`), reusing the event-stream row styling.

## Reversible?

Yes, fully additive. Revert drops `getHealth`, the three types, the `health`
state + its `init()` fetch, and the `04 / HOST` panel. Defaults (no `/api/health`
consumption) preserve today's behavior exactly.

## Impact

- The operator now sees, at a glance, the tool surface an agent will reach
  (MCP server names/transports) and the real sandbox posture (`FAIL-OPEN` is shown
  honestly — not greenwashed — so the no-Landlock caveat on this dev box is
  visible rather than hidden).
- Secrets stay server-side: `/api/health` emits name+type only; the panel renders
  exactly that. Confirmed at runtime — an `http` server's `url`/`headers` do not
  reach the client.
- **Integration boundary (honest):** the fetch/render is type-checked and
  build-verified; the panel reflects whatever `/api/health` returns, so its
  truth is the backend's, not a client copy.

## Test surface

- `tsc --noEmit` + `npm run build` green (build gates on tsc).
- Runtime curl of `/api/health` (default + `ORBITER_MCP_SERVERS` set) confirms the
  wire shape matches `Health` and that secrets are stripped.
- No new backend path → existing 57 backend tests unchanged (1 sandbox live-check
  skips by default).
