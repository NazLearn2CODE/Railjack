---
date: 2026-07-01
status: superseded
superseded_by: 2026-07-01-tool-approval-pretooluse-hook.md
---

# ADR: Tool execution runs with local autonomy (per-call approval deferred)

> **Superseded** by `[[2026-07-01-tool-approval-pretooluse-hook]]` — per-call
> approval is now live via a PreToolUse hook. The *finding* below (`can_use_tool`
> dormant under z.ai) remains accurate and is the foundation the new ADR builds on.

## Context

Milestone 2 attempted to wire operator-approved tool execution via the
`claude-agent-sdk` `can_use_tool` callback — dangerous tools (Bash/Edit/Write)
would surface an `approval_needed` event to the dashboard and block until the
operator approves/denies. The plumbing (callback, `approve_tool`, REST
`/approve` endpoint, dashboard `ApprovalCard`) was built end-to-end.

The gate does not fire in this environment.

## Finding

`can_use_tool` is never invoked by the CLI here. Verified empirically with a
minimal standalone repro (no FastAPI/scheduler):

- The SDK passes `--permission-prompt-tool stdio` correctly (captured the full
  spawned command: `claude --output-format stream-json --verbose ... --allowedTools
  ... --permission-prompt-tool stdio --input-format stream-json`).
- **0 callback calls** across:
  - CLI **2.1.139** (SDK-bundled) **and 2.1.191** (system `~/.local/bin/claude`,
    via `ClaudeAgentOptions(cli_path=...)`)
  - `permission_mode` ∈ {`default`, `plan`}
  - `setting_sources` ∈ {`[]` (isolated), default (load user/project)}
  - prompt delivered as both `query(str)` and `query(AsyncIterable)` (streaming)

The CLI runs Bash locally (a `ToolResultBlock` with the real stdout is returned)
without ever consulting the permission-prompt tool.

**Suspected cause:** the agent backend is **`glm-5-turbo` via the z.ai
Anthropic-compatible gateway** (`ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`
in `.env`, surfaced as `'model': 'glm-5-turbo[1m]'` in the CLI init message).
Tool-use round-tripped through that gateway appears to bypass the CLI's local
permission-prompt path. (Not server-side execution — the tool runs locally; the
permission check is simply skipped.)

## Decision

Run with **local autonomy**: the agent's `allowed_tools` is the full action set
(Read/Write/Edit/Bash/Grep/Glob/WebSearch/WebFetch), all auto-allowed. The
`can_use_tool` callback, `approve_tool`, the `/approve` endpoint, and the
dashboard approval cards remain in place but **dormant** (no-op until a backend
honors the gate). `setting_sources=[]` keeps the subprocess isolated from
`~/.claude` for predictable behavior.

Rationale: single-user, local-first tool on the operator's own machine with the
operator's own credentials — equivalent trust to running Claude Code directly.

## Reversible?

Yes. The approval plumbing is intact; it activates the moment the gate fires.

Upgrade paths to real per-call approval:
1. **Switch to the native Anthropic API** (a Claude model) — the CLI is expected
   to honor `--permission-prompt-tool stdio` against the real Anthropic backend.
2. **Expose `bash`/`edit`/`write` as custom SDK MCP tools** via
   `create_sdk_mcp_server` (in-process). The gateway cannot bypass in-process
   tools, so the approval gate lives in the tool handler. Remove native
   Bash/Edit/Write from `tools` so the model calls the gated versions.

Option 2 is backend-independent and the recommended long-term fix.

## Impact

- Agent can execute shell/file operations on this machine without per-call
  confirmation — acceptable for the local single-user trust model, not safe to
  expose to untrusted prompts without the sandboxing layers from the blueprint
  (L1–L4 security, still TODO).
- The dashboard's approval UI will not trigger under the current backend.
