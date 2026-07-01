---
date: 2026-07-02
status: accepted
---
# ADR: External MCP servers (blueprint §3.2 host/client)

## Context

The centralized topology (`[[2026-07-02-centralized-2dot-topology]]`) proved the
SDK's `mcp_servers` wiring path — but only for the in-process `orbiter` `delegate`
server. Blueprint §3.2 mandates MCP **host/client**: connecting agents to
*arbitrary external* MCP servers (filesystem, GitHub, custom stdio/SSE tools).
Today every agent is limited to the native tool set (Read/Write/Edit/Bash/…);
without external MCP, Orbiter can't grow its tool surface at runtime.

## Finding

`claude-agent-sdk` already ships full external-MCP client support:
`ClaudeAgentOptions.mcp_servers` accepts a `dict[str, McpServerConfig]` where each
spec is a plain dict — `McpStdioServerConfig` (`{type:"stdio", command, args?, env?}`),
`McpSSEServerConfig` / `McpHttpServerConfig` (`{type, url, headers?}`), or the
in-process `McpSdkServerConfig` already used for `delegate`. No new dependency, no
bespoke MCP framework (ponytail rung 5 — the installed dep already solves it).

The honest lazy change is therefore a **merge**, not a build: thread operator-supplied
external specs into the same `mcp_servers` dict the supervisor already constructs.

## Decision

- **`ClaudeSdkProvider(delegate=None, mcp_servers=None)`** — a new optional
  `mcp_servers: dict[str, dict]`. A pure static helper `_merge_mcp_servers(external,
  delegate_server)` merges external specs with the in-process `orbiter` server; the
  reserved `orbiter` key wins on collision; returns `None` when empty so the option is
  omitted entirely. **The `Provider` Protocol is unchanged** — external-MCP wiring is a
  concrete-impl concern (same logic the `delegate` ADR used); `FakeProvider` ignores it.
- **`app/main.py`** — `_load_mcp_servers()` parses `ORBITER_MCP_SERVERS` (JSON
  `{name: spec}`) once at import; the resulting dict is passed to the singleton
  `provider` (single-agent sessions) **and** to the team `worker_provider`. Bad JSON
  logs an error and yields `{}` so the gateway still boots. `GET /api/health` reports
  loaded servers as `{name, type}` only — never `env`/`headers`, which may carry secrets.

## Reversible?

Yes, fully additive. Reverting drops the `mcp_servers` param + helper from
`provider.py`, the `_load_mcp_servers`/`EXTERNAL_MCP`/health-key from `main.py`, and
the merge/env tests. Defaults (`mcp_servers=None`) preserve today's behavior exactly.

## Impact

- Agents (single, supervisor, **and** workers) can now use any operator-configured
  MCP server's tools at runtime — the tool surface grows without code changes.
- **Trust boundary (deliberate):** external MCP servers are operator-installed and
  run locally under the operator's control, so their tools are **trusted** and bypass
  the PreToolUse approval gate (which matches only `Bash|Write|Edit`) — the same
  posture as the `delegate` tool. *Trigger to revisit:* admit an untrusted/remote MCP
  server, then gate its tools too.
- **Integration boundary (honest):** the merge/wiring/config-parse is fully tested
  SDK-free; the *agent choosing* to call an external MCP tool is only exercisable with
  a real LLM + a running MCP server (like the `delegate` choice boundary).
- **Deferred (YAGNI, w/ triggers):** per-session/per-team MCP config (today it's
  global — local single-user OS, one config); a dashboard MCP-config UI (today env-only);
  gating untrusted MCP tools; `Channel` (second microkernel trait).

## Test surface

- `tests/test_provider.py` — `_merge_mcp_servers`: empty→None, external preserved,
  orbiter delegate coexists, orbiter key wins on collision.
- `tests/test_health_sandbox.py` — env→health surface (names+types), unset→empty,
  bad JSON→empty (boot-safe).
