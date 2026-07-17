Approved plan, executed with orchestration (I supervise + verify; grunt work delegated to a local model).

## Escalation policy (your new directive)
- Start each delegation on the local Ollama coder model.
- If it fails **>3 times** on the same task (wrong API, hallucination, won't compile, fails the test it claims), **stop retrying Ollama** and re-delegate that task to **glm-4.7** (via the z.ai gateway — the project's ambient provider, OpenAI-compatible at `/v1/chat/completions`). glm-4.7 is strictly more capable and project-aware-enough for the scoped chore.
- Either way, **I verify every output** (compile/test/read) before it lands. If glm-4.7 also fails, I do it myself.

## What gets delegated vs. kept
- **I keep:** the memory/`setting_sources` reversal, the model-routing design, cwd threading, all architecture decisions, and final verification.
- **I delegate (self-contained, verifiable):** drafting tests I've fully specified, doc prose drafts from my bullet points, mechanical edits I've spelled out.

## The work (unchanged, approved)
1. **registry.py** — `DEFAULT_MODEL="glm-4.7"` + `default_model()`; `resolve()` untouched (tests pin it).
2. **main.py** — fill `model = req.model or registry.default_model(req.provider)` before `resolve()` in session + team creation.
3. **provider.py** — add `cwd` to `ClaudeSdkProvider`; flip `setting_sources=[]`→`["project"]`; pass `cwd=self._cwd`.
4. **agent.py + orchestrator.py** — thread cwd (PROJECT_ROOT / WORKSPACE_ROOT; orchestrator reads `worker_provider._cwd`).
5. **Sidebar.tsx** — select (draft state) + APPLY button + applied-model readout.
6. **Composer.tsx** — replace duplicate select with read-only `MODEL ▸ X` chip; auto-select glm-4.7 on init (drop fake AMBIENT option).
7. **Tests** — default_model unit; ambient-fill on create_session; provider cwd/setting_sources capture (monkeypatched-query seam).
8. **Verify** — pytest · ruff · tsc · build; live smoke (restart uvicorn, ask agent about CLAUDE.md — must now know it).
9. **Docs** — index.md, CodeCompass.md, new ADR `2026-07-09-project-memory-and-model-honesty.md`, session log.

Starting execution now.