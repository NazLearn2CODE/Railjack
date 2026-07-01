---
date: 2026-07-01
status: accepted
supersedes: 2026-07-01-tool-execution-local-autonomy.md
---

# ADR: Per-call tool approval via a PreToolUse hook

## Context

`[[2026-07-01-tool-execution-local-autonomy]]` established that `can_use_tool`
is dormant under the z.ai/GLM backend, so the agent ran with local autonomy and
per-call approval was deferred. That ADR listed two upgrade paths: switch to the
native Anthropic API, or expose bash/edit/write as custom SDK MCP tools. This
ADR implements real per-call approval via a **third path** neither anticipated.

## Finding

A source read of `claude-agent-sdk` v0.1.81 plus one live spike against z.ai
resolved how the SDK's control protocol actually behaves there:

- **`can_use_tool` is the *permission* subtype** of the CLI↔SDK control protocol
  (`permission_prompt_tool_name="stdio"`). Dormant under z.ai (0 callbacks — see
  the prior ADR).
- **`PreToolUse` hooks are a *different* subtype** (`hook_callback_request`) on
  the same protocol, and they **DO fire under z.ai.** Verified live: a
  PreToolUse hook fired for `Bash`, returned `permissionDecision: "allow"`, and
  the native Bash tool then executed. The hook callback is an async function in
  the gateway process, so it can `await` an operator-approval future and return
  `allow`/`deny`.
- **The prescribed SDK-MCP path is a dead end under z.ai.** A custom SDK MCP
  tool (`create_sdk_mcp_server`) was blocked — the model reported *"permission
  not granted, tool call blocked."* SDK MCP tools default to the "ask" permission
  path, which can't resolve while the permission tool is dormant. So although MCP
  execution routes in-process (source-verified: `_handle_sdk_mcp_request`), the
  CLI never reaches it. Implementing it would have required *both* fighting the
  permission layer *and* reimplementing bash/write/edit.

Net: the only control-protocol subtype z.ai skips is the permission one. Hooks
and MCP-execution use other subtypes — hooks work, MCP is blocked upstream.

## Decision

Gate the **native** Bash/Write/Edit tools with a **PreToolUse hook** rather than
the SDK permission callback or custom MCP tools:

- `ClaudeAgentOptions.hooks = {"PreToolUse": [HookMatcher(matcher="Bash|Write|Edit", hooks=[...])]}`.
- The hook emits the existing `approval_needed` event, parks a future in
  `pending_approvals`, and resolves via the existing `approve_tool()` →
  `/api/sessions/{id}/approve` → dashboard `ApprovalCard` plumbing (built in the
  prior milestone, dormant until now).
- **Fail-closed:** `asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT=600s)`
  denies on operator silence — an idle operator never lets a blocked tool
  through. Denies carry `permissionDecisionReason` back to the model.
- Read-only tools (Read/Grep/Glob/WebSearch/WebFetch) carry no hook → auto-run.
- `can_use_tool` removed (was dormant); `setting_sources=[]` retained.

Rationale: shortest diff, reuses full-fidelity native tools (no reimplementation),
and the gate is real because the hook subtype fires under z.ai.

## Reversible?

Yes — remove the `hooks` dict from `ClaudeAgentOptions` to revert to full local
autonomy (the prior ADR's state). All approval plumbing stays in place.

## Impact

- Real per-call approval of dangerous tools now works under the z.ai backend.
  **Browser-verified 2026-07-01** (smoke test, `/tmp/orbiter-smoke`): agent calls
  `Bash` → approval card renders in the dashboard → operator APPROVE → native Bash
  executes → result streams back, end-to-end. Upgrades this ADR's "hooks fire" claim
  from a code spike to a verified UI loop.
- Read-only tools execute without confirmation (intended — safe, and keeps the
  agent from stalling on every Read/Grep).
- 600s approval timeout denies (fail-closed); raise `APPROVAL_TIMEOUT` for more
  decision time. The L1–L4 sandboxing layers from the blueprint remain TODO.
