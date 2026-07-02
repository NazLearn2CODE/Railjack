---
date: 2026-07-02
status: accepted
---
# ADR: delegate_many verified end-to-end under z.ai/GLM (+ two latent fixes)

## Context

`[[index]]`'s NEXT INCREMENT was to verify `delegate_many` under the real z.ai/GLM
backend — the one honest risk in `[[2026-07-02-delegate-many-fanout]]`: *will the
GLM model fill the `delegations: [{role,task}…]` array-of-objects schema and
actually fan out?* The OS primitive was SDK-free proven (59 tests, incl. a
concurrency-overlap proof); the supervisor-LLM half was not. This ADR records the
verification **and** the two latent bugs it surfaced (all prior tests run SDK-free
via `FakeProvider`, which never exercises the real CLI — so both shipped green-but-broken).

## Drive + evidence

A WS driver (the gateway's own socket surface — `POST /api/teams` →
`ws://localhost:8000/ws/sessions/{id}`) dispatched a TEAM run forcing a 2-worker
fan-out (researcher + coder; coder writes `/tmp/orbiter-fanout-verify.txt`). All
three claims **PASS**:

- **(a) Schema** — the supervisor emitted `mcp__orbiter__delegate_many` with
  `delegations` = a 2-element `[{role:researcher,…},{role:coder,…}]` array
  (well-formed `{role,task}` objects). The array-of-objects risk is closed.
- **(b) Concurrency** — 2 distinct worker ids under one `fanout_id`, overlapping
  active windows (researcher 3.6s–6.8s, coder 3.6s–26.1s → coder started before
  researcher finished). `asyncio.gather` fan-out confirmed under the real LLM.
- **(c) Worker approval** — the coder's gated tool surfaced as a nested
  `worker_event{event:approval_needed}`; `POST /api/sessions/{workerId}/approve`
  resolved it; the gated Bash executed; the file was written; one HMAC receipt
  minted (`logs/receipts.jsonl`). End-to-end through the real gateway.

Browser drive (Playwright on the rendered dashboard) confirmed the grouped
`▾ FAN-OUT` block renders with worker lanes (`DELEGATED · role`) and the
`delegate_many` TOOL row (the delegations JSON) — detected in the live DOM. The
inline `ApprovalCard` pixel was not captured this session: under z.ai the coder's
choice to invoke a gated tool is nondeterministic and on the browser runs it
answered in text instead of writing (no gate → no card). The card's data flow is
proven by the WS run above and its rendering is inspection-verified
(`Message.tsx` renders `lane.approval` via the shared `ApprovalCard` bound to
`approveWorker`, which POSTs `/api/sessions/{workerId}/approve` and clears the
lane on click) + tsc/build-green; a clean screenshot is the only deferred pixel.

## Finding — two latent bugs (root cause, fixed)

1. **Supervisor/worker `session_id` must be a valid UUID.** `orchestrator.py`
   built `f"supervisor-{hex8}"` / `f"worker-{role}-{hex8}"`; the CLI (2.1.197)
   rejects a non-UUID `--session-id` at init with `Error: Invalid session ID.
   Must be a valid UUID.` (exit 1, before any LLM call). Plain sessions worked
   only because `manager.create_session` already used `str(uuid.uuid4())`.
   **Fix:** `str(uuid.uuid4())` for both. Nothing depended on the prefix — `kind`
   carries supervisor/worker, the role rides in the `worker_event` frame, and the
   dashboard renders `row.role`. (Tests that asserted the old prefix updated to
   assert the UUID format — which is the actual requirement.)
2. **In-process MCP tools are exposed as `mcp__<server>__<tool>`.** The
   supervisor's `allowed_tools` carried the bare `"delegate_many"` (orchestrator),
   but the CLI surfaces the in-process `orbiter` server's tool as
   `mcp__orbiter__delegate_many`; the bare name doesn't match → the CLI **denies
   the call as unpermitted** ("requested permissions to use
   mcp__orbiter__delegate_many, but you haven't granted [it]"). The SDK joins
   `allowed_tools` straight into `--allowedTools` (no MCP auto-allow).
   **Fix:** `ClaudeSdkProvider.stream()` expands `allowed_tools` with the
   namespaced name when the `orbiter` server is registered (MCP-naming knowledge
   stays in the provider — the SDK-coupling layer — not orchestrator). A constant
   `_DELEGATE_SERVER`/`_DELEGATE_MANY_TOOL` removes the rename footgun.
3. **`delegations` may arrive stringified.** Under z.ai the model occasionally
   passed `delegations` as a JSON *string* rather than an array (observed in the
   captured frame). **Fix:** coerce at the LLM-facing tool boundary
   (`isinstance(str)` → `json.loads`), so a valid string still fans out and a
   malformed one becomes empty.

## Reversible?

Yes — all additive/guarded. (1) revert to prefixed ids (re-breaks the CLI); (2)
drop the namespaced expansion (re-breaks permission); (3) drop the coercion (a
stringified array would iterate chars). Defaults preserve prior SDK-free behavior.

## Impact

- Closes the `delegate_many` integration boundary named in
  `[[2026-07-02-delegate-many-fanout]]`: a supervised TEAM run now actually fans
  out under z.ai/GLM, renders as a grouped block, and a gated worker lane is
  inline-approvable. Centralized 2DOT route 2 is runtime-confirmed, not just
  SDK-free tested.
- Generalizes: **any** future in-process MCP tool must list its `mcp__server__tool`
  name (the provider now centralizes this), and **any** CLI-backed session id must
  be a UUID — both are easy to re-break; the comments + constants flag it.
- Security-floor observation (not a bug): the coder's `/tmp` **Write** was
  L1-hard-denied (the server's writable root is its `TMPDIR`, a per-session subdir
  under the harness — not `/tmp`); it fell back to a Bash redirect, which L1 does
  not path-check (only L2 catastrophic) → reached approval → allowed. This is the
  documented Bash-vs-Write confinement asymmetry; L3 Landlock is the backstop and
  is fail-open on this dev box (`[[2026-07-01-sandbox-l3-landlock]]`). Two HMAC
  receipts (deny + allow) were minted — the L4 ledger operates correctly under a
  real fan-out.
- Deferred: a clean browser screenshot of the inline `ApprovalCard`; native
  Anthropic re-verification (`[[2026-07-02-native-anthropic-verification-deferred]]`).

## Test surface

- No new SDK-free tests (the bugs are CLI-integration paths; FakeProvider can't
  reach them — that's why they shipped latent). Existing 59 stay green; the 3
  that asserted the old prefixed-id format now assert the UUID format.
- The drive IS the integration test. Replay: boot `.venv/bin/uvicorn app.main:app
  --port 8000`, then the WS driver at `/tmp/orbiter_verify/driver.py` (captures
  every frame to `frames.jsonl` + auto-approves the coder's gate).
