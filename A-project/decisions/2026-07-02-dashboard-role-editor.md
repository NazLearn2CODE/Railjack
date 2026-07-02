---
date: 2026-07-02
status: accepted
---
# ADR: In-dashboard role editor (TEAM mode → custom rosters)

## Context

The centralized topology (`[[2026-07-02-centralized-2dot-topology]]`) was surfaced
to the dashboard via the `○/● TEAM` composer toggle, but the operator could only
ever dispatch the hardcoded `DEFAULT_ROLES` roster (researcher + coder) — the
dashboard never exposed the `roles` param that `POST /api/teams` already accepted.
The follow-on queue listed "in-dashboard role editor" as the next item. Separately,
the *other* queued item — a dedicated multi-lane team view — was investigated and
ruled **YAGNI**: `Team.delegate()` awaits one `worker.run()` per tool call, so
delegations are sequential (centralized hub-and-spoke) and there is no concurrent
worker activity to display side-by-side; the inline `worker_lane` panels already
represent sequential delegation. That view's real trigger is a `delegate_many`
fan-out primitive, which doesn't exist.

## Finding

The role editor is a **pure UI thread** over an existing API surface — no backend
change. The honest lazy shape: a team-only reveal toggle in `Composer` (mirroring
the `+ SYS PROMPT` pattern already there) opens a panel that edits a local
`RoleSpec[]`; that array is passed straight through `dispatch → createTeam`. Empty
roster → server `DEFAULT_ROLES`; any roles → hired verbatim — the panel simply makes
the server's existing semantics visible and editable.

## Decision

- **`web/src/types.ts`** — `RoleSpec { name, system_prompt }` (the `POST /api/teams`
  `roles` shape). `api.ts` `createTeam` now types against it.
- **`web/src/store.ts`** — `dispatch(prompt, systemPrompt?, roles?)`; in team mode
  passes `roles.length ? roles : undefined` to `createTeam` (undefined → defaults).
- **`web/src/components/Composer.tsx`** — `roles[]` + `rolesOpen` **local state**
  (persists across dispatches like `sys`, since nothing else reads it). A team-only
  `+ ROLES` toggle reveals a panel: per-role `name` + `system_prompt` inputs,
  add/remove. Roles with an empty `name` are dropped on dispatch (never hire a
  nameless worker); empty roster shows `DEFAULT TEAM ▸ researcher + coder`. The
  closed-toggle label shows the named-role count (`▾ ROLES · 2`) so the operator
  sees their roster is live.

## Reversible?

Yes, fully additive. Revert drops `RoleSpec`, the `roles` param on `dispatch`, the
`createTeam` typing, and the `Composer` editor. Defaults (no `roles` passed)
preserve today's behavior exactly — `createTeam` still sends `roles: null`.

## Impact

- The operator can now compose an arbitrary specialist roster (e.g.
  architect + tester, writer + reviewer) from the dashboard, no code/API change.
- **Honest boundary:** an empty-name role row is silently dropped on dispatch rather
  than hired as `""` — forgiving, no validation UI. Confirmed by curl: custom
  `[{architect},{tester}]` → response `roles: ["architect","tester"]`; `null` →
  `["researcher","coder"]`.
- **Ponytail ceiling:** the small role list uses index keys — focus may jump when
  removing a middle row while typing. Fine for a 1–3 item roster; upgrade to stable
  ids if it grows.
- **Deferred (YAGNI, w/ trigger):** per-role `allowed_tools` editing (today the API
  ships name+system_prompt only; the server applies `WorkerRole`'s default
  allowed-tools — add when an operator needs to restrict a role's tool set); the
  multi-lane team view (trigger: concurrent `delegate_many` fan-out).

## Test surface

- `tsc --noEmit` + `npm run build` green (build gates on tsc).
- Runtime `POST /api/teams` (custom roles vs `null`) confirms the wire shape and the
  hired-vs-default semantics the UI threads.
- No backend path changed → existing 57 backend tests unchanged (1 sandbox live-check
  skips by default).
