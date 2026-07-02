---
date: 2026-07-02
status: accepted
---
# ADR: Shared team token-budget pool

## Context

`delegate_many` fan-out (`[[2026-07-02-delegate-many-fanout]]`, verified
end-to-end `[[2026-07-02-delegate-many-verified]]`) made the existing per-session
token budget **wasteful and unbounded in aggregate**. `TokenBudgetManager`
(`app/core/scheduler.py`) keyed usage on `session_id` against a single
`default_ceiling` (200k). Because the supervisor and **each** worker carry a
distinct `session_id`, a 2-worker fan-out ran with **3 independent 200k ceilings**
— no single number bounded the team's total spend, and a cheap worker's unused
headroom could never subsidize a costly one. The index flagged this as the next
increment once fan-out was runtime-confirmed (it now is).

## Finding

The budget manager already keys accounting on an opaque string. A shared team
pool is therefore "all team members bill under one key" — the only missing piece
is a **per-key ceiling** (the pool must be team-sized, not 200k, or the
supervisor alone would exhaust it). Two ways to thread the shared key:

1. **Register a named pool** on the budget manager + give `AgentSession` an
   optional `budget_key` seam; `Team` mints one key, sets a team-sized ceiling,
   and stamps it on supervisor + every worker.
2. **A separate `TeamBudget` class** wrapping its own usage map / ceiling.

Option 2 duplicates `TokenBudgetManager` and bypasses the scheduler's existing
single mid-turn enforcement point (`consume` in `AgentSession.run`). Option 1
reuses it: the team pool flows through the *same* `consume` / `check_budget` /
`enter_turn` gate, so admission still bills the pool and the over-budget break
still fires. The lazy one.

## Decision

- **`TokenBudgetManager`** (`app/core/scheduler.py`) — gains a per-key ceiling
  override map: `set_ceiling(key, n)` + `effective_ceiling(key)`. `check_budget`
  consults the map before `default_ceiling`. The previously-dead `ceiling`
  parameter on `check_budget` (no caller ever passed it) is removed — deletion
  over addition. `consume`/`check_budget` params renamed `session_id` → `key` to
  reflect that the string is now a budget key, not necessarily a session id.
- **`AgentSession`** (`app/core/agent.py`) — gains `budget_key: Optional[str]`.
  `run()` resolves `budget_key = self.budget_key or self.session_id` once and
  uses it for `enter_turn`, `consume`, **and** the over-budget error message
  (via `effective_ceiling`, so the message reports the *real* ceiling — team
  pool or default — not a stale 200k).
- **`Team`** (`app/core/orchestrator.py`) — mints `self.budget_key =
  "team-<hex8>"` at construction. `supervisor()` **establishes the pool**:
  `team_budget_ceiling or default_ceiling * (1 + len(roles))`, then
  `set_ceiling(self.budget_key, ceiling)`, and stamps `budget_key` on the
  supervisor. `_run_worker` stamps the same `budget_key` on every worker. So the
  whole fan-out — supervisor + all its workers, across every `delegate_many`
  call in the session — bills against one team-sized pool.

**Ceiling sizing:** default scales with hired breadth (supervisor + N roles): a
default 2-role team gets 600k. `team_budget_ceiling` overrides it for tests /
tight pools. `# ponytail:` breadth is proxied by *hired roles*, not fan-out
cardinality — a role delegated twice reuses its slice; revisit if real fan-outs
overshoot. No env knob added (YAGNI; add `ORBITER_TEAM_BUDGET` when a run needs
to override it).

## Reversible?

Yes, fully additive. Plain sessions are untouched (`budget_key=None` → keys on
`session_id`, falls through to `default_ceiling` — identical to before). Revert
drops: the `ceilings` map + `set_ceiling`/`effective_ceiling` + the renamed
params on `TokenBudgetManager`; the `budget_key` field + the one resolved local
in `run()` from `AgentSession`; the `team_budget_ceiling` param, `budget_key`
field, and the two stamps + `set_ceiling` call from `Team`. With no `budget_key`
stamped, every session bills independently again exactly as before.

## Impact

- A fan-out is now **bounded as a whole**: supervisor + workers share one
  ceiling, so the team cannot collectively exceed it; an under-budget worker's
  headroom is available to a costlier sibling. This is the behavior the index
  asked for now that fan-out is runtime-confirmed.
- The over-budget path a worker already took (`_run_worker` returns a string the
  supervisor acts on) is unchanged — only the *key* it bills against changed.
  Existing `test_delegate_many_worker_over_budget_returns_error_string` still
  passes: it never calls `supervisor()`, so no team ceiling is set and the worker
  falls through to `default_ceiling` (per-session), as before.
- **Lifecycle:** pool entries accumulate in `TokenBudgetManager.usage` /
  `.ceilings` for the process lifetime — same retention as session usage, no GC
  added (local single-user dev OS; YAGNI).
- **Integration boundary:** the shared-pool accounting is SDK-free tested via
  `FakeProvider`; the *supervisor LLM staying within a team budget under real
  load* is a real-LLM quantity question, not a logic one (no new tool/schema
  boundary introduced here).

## Test surface

- `tests/test_scheduler.py` — `test_set_ceiling_overrides_default_for_one_key_only`:
  a per-key ceiling overrides the default for that key only; other keys keep
  `default_ceiling`; `effective_ceiling` reports both correctly.
- `tests/test_orchestrator.py` — `test_team_fan_out_shares_one_budget_pool`:
  two workers each consuming 60 tokens against a 100-token team pool → exactly
  one goes over budget (combined 120 > 100). Deterministic because `consume` is
  an atomic sync block: the first caller lands at 60 (under), the second at 120
  (over). Under independent budgets both would succeed — the red state ruled out.
- 61 pytest green (+2), ruff clean, `tsc --noEmit` + `npm run build` clean
  (frontend untouched).
