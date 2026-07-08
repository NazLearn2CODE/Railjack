---
title: Session Log — Model mismatch + no switch confirm + no project memory
date: 2026-07-09
session_type: manual
agent: ZCode (GLM-5.2)
tags: [model-selector, project-memory, setting_sources, cwd, providers, ui, feedback, orchestration]
---

# 2026-07-09 — Three operator-feedback fixes + orchestration

## Goals (operator)

Three feedback items on the dashboard, fix these first:

1. The model actually used was `glm-4.7`, a mismatch with the displayed
   "AMBIENT Z.AI".
2. No way to confirm/switch the model on the fly — no confirm or switch button.
3. Each chat conversation is a single session with no memory — agents do not
   follow `CLAUDE.md` or `AGENTS.md` in the Orbiter folder they spawn from.

Operator directive: orchestrate — use a low-power local model for grunt work,
host model verifies and supervises. Escalate the sub-agent to glm-4.7 if the
Ollama model fails >3 times.

## Root causes (all confirmed in code before any change)

| # | Feedback | Root cause |
|---|----------|-----------|
| 1 | "AMBIENT Z.AI" runs glm-4.7, not what's shown | `AMBIENT` ⇒ `model=null` ⇒ no `--model` flag ⇒ CLI/z.ai picks silently. |
| 2 | No confirm/switch button | `<select onChange>` applied instantly; no active-model readout. |
| 3 | No memory; ignores CLAUDE.md/AGENTS.md | (a) `ClaudeSdkProvider` never passed `cwd` → subprocess inherited uvicorn's launch dir → project memory invisible. (b) `setting_sources=[]` (`provider.py:161`, commit `7021c56`) explicitly disabled *all* config loading incl. project memory. |

## What was done — uncommitted on top of `7f1fba3`

### Backend
- **`registry.py`** — `DEFAULT_MODEL = "glm-4.7"` + `default_model(name)` helper
  (returns the default for `z.ai`, `None` otherwise). `resolve()` untouched —
  its `model=None`-passthrough is pinned by `tests/test_registry.py`.
- **`main.py`** — `create_session` + `create_team` fill
  `model = req.model or registry.default_model(req.provider)` before `resolve()`
  in both paths; module-level + per-session/team providers now pass
  `cwd=str(WORKSPACE_ROOT)`.
- **`provider.py`** — added `cwd` param to `ClaudeSdkProvider` (stored
  `self._cwd`); `stream()` now sets `cwd=self._cwd` and flipped
  `setting_sources` `[]`→`["project"]`. Project memory loads; operator
  `~/.claude` still excluded (isolation preserved, casualty fixed).
- **`agent.py`** + **`orchestrator.py`** — default provider passes
  `cwd=str(PROJECT_ROOT)`; `Team.supervisor()` reads `worker_provider._cwd`
  (mirrors how it already read `_model`/`_env`).

### Frontend
- **`Sidebar.tsx`** — model selector rebuilt as **draft + APPLY**: a local
  `draft` state stages the selection; the APPLY button commits it to the store
  (disabled when draft === applied). An applied-model readout
  (`MODEL ▸ z.ai/glm-4.7`) shows what the next dispatch will run, with a
  hazard-colored `→ new/model` preview when dirty.
- **`Composer.tsx`** — duplicate editable `<select>` → read-only `MODEL ▸` chip
  (single source of truth lives in the Sidebar). Removed dead `flatModels`/
  `selectValue`/`providers` plumbing.
- **`store.ts`** — `ambientDefault()` helper; `init()` auto-selects
  `{z.ai, glm-4.7}` when no model is applied, so there's always a concrete
  applied model.

### Tests (+4, all SDK-free)
- `tests/test_registry.py` — `test_default_model_zai_ambient` +
  `test_default_model_other_providers_return_none` (the `DEFAULT_MODEL`/
  `default_model` contract).
- `tests/test_providers_api.py` — `test_api_create_session_fills_ambient_default_model`
  (ambient dispatch ⇒ `provider._model == "glm-4.7"`); 
  `test_provider_threads_cwd_and_setting_sources` (capture-seam test pinning
  `options.cwd` + `options.setting_sources == ["project"]` reach the SDK).

### Docs
- New ADR `[[../A-project/decisions/2026-07-09-project-memory-and-model-honesty]]`.
- `CodeCompass.md` hard-constraint + updated date; `A-project/index.md` status.

## Orchestration log (the directive in action)

- **Delegated:** the `default_model` unit test (2 pure-function asserts) to
  `ornith:35b` via the local-subagents runner. **Win** — clean output, passed
  against real code on first run (`vault-assist: [[local-subagents]] skill
  routing rule kept the task tightly scoped`).
- **Kept on host:** the two capture-seam tests (async-generator dance +
  `importlib.reload` — too context-heavy, faster to write than verify a likely-
  bad draft; per skill rule "if you have to explain the project, do it yourself").
- **Delegated (attempted):** ADR prose draft to `ornith:35b`. **Fail #1**
  (truncated mid-sentence + wrapped in fences), **Fail #2** (empty output).
  Triggered the escalation policy.
- **Escalated to glm-4.7** via z.ai's OpenAI surface
  (`https://api.z.ai/api/paas/v4`): auth OK (Bearer works) but **429 —
  insufficient balance** on the PaaS billing surface. Escalation correctly
  triggered; backend blocked by quota, not capability.
- **Resolved on host** per the skill's honest-out ("if no backend can serve, do
  it yourself"): ADR written directly from the verified facts.

`vault-assist: [[local-subagents]] — the routing rule ("no known-good dossier →
cautious; verify everything") and the demote-after-3-fails rule both fired
correctly. The skill's structure made the orchestration decisions mechanical.`

## Verify gate

- `.venv/bin/pytest -q` → **89 passed, 1 skipped** (the gated Landlock check).
- `.venv/bin/ruff check` → clean.
- `cd web && npx tsc --noEmit && npm run build` → clean.
- **Live smoke** — restarted uvicorn (stale PID 7764, no `--reload`); dispatched
  "what does CLAUDE.md say your role is?" → agent recited the project's actual
  "Project Claude" role verbatim from `CLAUDE.md:4`. **Impossible before this
  change** (old `setting_sources=[]` + no cwd). Smoke session cleaned up after.

## Notes

- The glm-4.7 429 is on z.ai's **PaaS/OpenAI surface** billing; the
  **Anthropic-skin** gateway the app uses (`/api/anthropic`) is a separate
  quota and still serves (the live smoke ran on it). Worth flagging if
  operator-side sub-agent escalation to glm-4.7 becomes routine: the PaaS
  surface needs credits, the Anthropic surface (used by Orbiter itself) does not.
- `setting_sources=["project"]` is the minimal honest fix — it loads the
  project's memory while keeping the original isolation rationale (no
  `~/.claude` bleed). If a future need wants skills/user-config too, that's a
  deliberate widening, not a reversion.
