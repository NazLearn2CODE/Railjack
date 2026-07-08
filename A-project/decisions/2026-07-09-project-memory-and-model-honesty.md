---
date: 2026-07-09
status: accepted
---

# ADR: Project memory + honest model selection

## Context

Orbiter's dashboard surfaced three operator complaints. The model picker
displayed "AMBIENT · Z.AI" but the agent actually ran `glm-4.7` — a silent
mismatch between the UI and the wire. There was no confirmation that a model
switch took effect. And every chat was a single session with no memory: agents
ignored the project's `CLAUDE.md` and `AGENTS.md`.

Root cause: the AMBIENT state sent `model=null`, so no `--model` flag reached
the CLI and z.ai's gateway silently chose `glm-4.7`. The no-memory bug had two
compounding causes. First, `ClaudeSdkProvider` never passed `cwd` to the SDK, so
the subprocess inherited uvicorn's launch directory and could not find project
memory. Second, `setting_sources` was hardcoded to `[]`, disabling all config
loading including the project's `CLAUDE.md`.

## Decision

- **Pin the ambient default:** `registry.DEFAULT_MODEL = "glm-4.7"` plus a
  `default_model(name)` helper; `create_session` and `create_team` fill
  `model = req.model or registry.default_model(req.provider)` before `resolve()`
  so AMBIENT always resolves to a concrete model. `resolve()` itself is unchanged
  (existing tests pin its shape).
- **Add a `cwd` parameter to `ClaudeSdkProvider`**, threaded from
  `WORKSPACE_ROOT` through `main.py`, `agent.py`, and `orchestrator.py` so the
  SDK subprocess runs in the project root and discovers project memory.
- **Flip `setting_sources` from `[]` to `["project"]`** — load the project's
  `CLAUDE.md`/`AGENTS.md` but still exclude the operator's `~/.claude`
  (isolation preserved, casualty fixed).
- **Frontend Sidebar:** a draft + APPLY model selector with an applied-model
  readout; the Composer's duplicate selector became a read-only `MODEL ▸` chip.
- **The store auto-selects `glm-4.7` on init** so there is always a concrete
  applied model (display === reality).

## Rationale

**Honesty:** the displayed model must equal the model that runs. A silent
gateway guess erodes trust in an observability-focused tool. The
`setting_sources=[]` choice (commit `7021c56`, retained in ADR
`2026-07-01-tool-approval-pretooluse-hook`) was made for isolation while
debugging why the `can_use_tool` gate did not fire under z.ai/GLM — but
excluding project memory was an unintended casualty, not the intent.

`["project"]` keeps the isolation (no `~/.claude` bleed) while restoring project
rule-loading. `cwd` is scoped to `WORKSPACE_ROOT`, so re-pointing the workspace
root re-scopes which project's memory the agent loads — consistent with the
cephalon probe on `/api/health`.

## Consequences

- Agents now follow project memory: a dispatch asking "what does `CLAUDE.md`
  say?" returns the project's real instructions (verified live — the agent
  recited its "Project Claude" role verbatim from the file).
- The picker's displayed value is authoritative; switching requires an explicit
  APPLY click.
- No new endpoints and no `Provider` Protocol change (`cwd` is a concrete-impl
  concern, like `env` and `model`).
- A project lacking a `CLAUDE.md` loads nothing project-side without error —
  now intentional rather than forced.

## Deferred

- **Cross-turn conversational memory** (`resume`/`continue` a prior
  `session_id`): this ADR restores *project-rule* memory within one dispatch;
  continuity across dispatches is a larger separate increment using the SDK's
  `resume`/session-store.
- **Live-switching the model of an already-running session**; APPLY sets the
  default for the next dispatch only, matching the store's per-dispatch
  resolution.
- **Per-provider ambient defaults beyond z.ai**; a configured 2nd provider with
  no model chosen stays `None` and is rejected rather than guessed.
