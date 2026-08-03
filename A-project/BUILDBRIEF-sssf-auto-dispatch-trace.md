# BUILDBRIEF — sssf trace layer on Kanban auto-dispatch (Phase A)

Repo: `/var/home/NAZ/Coding Projects/Railjack`. Read `CLAUDE.md`. Target: `app/kanban.py`.
Engine source to port: `~/Coding Projects/sssf-pilot/tracer.py` (proven, stdlib-only).

## Goal (Phase A — observability only, no behavior change)
Every auto-dispatch run emits a structured trace to SQLite, so a dispatched task's full
lifecycle is queryable after the fact. Today only the live activity feed shows what happened
(per-task, auto-expiring) — there's no retained, queryable record. This adds one, additively.
**Do not change dispatch/reaper/watchdog logic** — only instrument it.

## 1. Shared engine module
Copy `~/Coding Projects/sssf-pilot/tracer.py` → `app/factory/tracer.py` (new package
`app/factory/`, empty `__init__.py`). Keep it stdlib-only (sqlite3, json, uuid, datetime).
The Tracer writes a **separate trace db** (don't bloat `kanban.db`): `app/factory/sssf.db`
(WAL). Path via a small `_tracer()` helper that lazy-inits a module-level singleton.

## 2. Instrument `app/kanban.py` (additive calls only)
Map the existing lifecycle to trace events (line numbers are current anchors):
- **`start_task()` (~614), Mode 1 dispatch branch:** after deciding to dispatch + before
  `Popen` (~676): `tr.session_start(adw_id=f"task-{task_id}", adw_name=task_title,
  request=task_description)`; `phase_id = tr.phase_start(adw_id, seq=1, name="worker",
  kind="agent", owner="claude")`; stash `phase_id` alongside the `_workers[task_id]` tuple
  (extend the tuple to `(proc, started_monotonic, adw_id, phase_id)`); `tr.event(adw_id,
  phase_id, "dispatch", "claude -p", {"prompt_len": len(prompt)})`.
- **Reaper loop (~302–311), on worker exit:** when a worker `proc.poll()` is not None and
  you pop it from `_workers`: `tr.phase_end(phase_id, "pass" if rc==0 else "fail",
  error=None if rc==0 else f"exit {rc}")`; `tr.session_end(adw_id, "accepted" if rc==0 else
  "failed")`. Also emit `tr.event(adw_id, phase_id, "worker_exit", str(rc))`.
- **`_watchdog_kill()` (~269):** before/after the kill, `tr.event(adw_id, phase_id,
  "watchdog_kill", "max-runtime", {"minutes": elapsed})` and `tr.phase_end(..., "fail",
  error="watchdog: exceeded max-runtime")` + `tr.session_end(adw_id, "failed")`.
- **`post_activity()` (~721):** optionally `tr.event(adw_id, phase_id, "activity", line)`
  so the agent's self-reported progress is in the trace too. (Needs the task→(adw_id,phase_id)
  lookup; cheap via a small dict mirroring `_workers`.)

## 3. A read-only surface (tiny)
Add one function `trace_for_task(task_id) -> dict` returning that task's session + phases +
events + gate_results (Phase B) from `sssf.db`, and wire a `GET /api/kanban/trace/{task_id}`
route returning it (or stash it for the panel later). Phase A only needs the data captured;
the UI can come after.

## What's deliberately OUT (Phase B — separate brief)
- **Gates.** Need (a) worker stdout/stderr captured (currently `DEVNULL` at ~679) and (b)
  resumable workers (`claude --resume`) so a failed gate can correct the *same session*
  instead of cold re-dispatching. That's a worker-model change, not instrumentation — defer.

## Acceptance
1. Dispatch a throwaway task via ▶; after the worker exits, `sqlite3 app/factory/sssf.db`
   shows: 1 session (status accepted/failed), 1 phase (worker), ≥2 events (dispatch, worker_exit).
2. Force a watchdog kill (cap a tiny max-runtime on a slow task) → trace shows a
   `watchdog_kill` event + phase `fail`.
3. **Zero behavior change:** dispatch timing, reaper scoping (no `waitpid(-1)`), the activity
   feed, and task column moves all behave exactly as before. The trace is write-only sidecar.
4. `pytest` still green (instrumentation is additive; mock/ignore the Tracer in existing
   kanban tests if it needs a db path — point `_tracer()` at a tmp path under test).
