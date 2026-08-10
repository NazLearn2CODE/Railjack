# Fireside Mode — Frontend Build Brief (Slice 2)

> **For:** agy (builder). **Verifier:** Tawhan (host). **Status:** ready to build.
> **Backend (Slice 1) is DONE + verified** — call its API contract below. Do not edit backend.
> Source design: `~/.claude/plans/so-let-s-be-even-rustling-hennessy.md`.

## Context
Add a third Story Scout mode — **FIRESIDE** — to `frontend/src/components/ThailandNowPanel.tsx`,
with two sub-flows: **SOURCE TOPICS** (async job → topic cards) and **EDIT NOTES** (sync → notes block).
Ben-anchored Fireside show; Naz sources topics + gives edit notes.

## Backend API contract (already built — call, don't change)
- `POST /api/thailandnow/scout/fireside/source` — body `{seed?: string, category?: string}` → `{id: string}` (job id). Single-flight (409 if one running).
- Poll: `GET /api/thailandnow/jobs` → find job with `kind === "fireside-source"`; when `status === "done"`, fetch:
- `GET /api/thailandnow/scout/fireside/source/report/{jid}` → `{topics: Topic[], mode: "notebook"|"web-fallback", notebook_id}`.
  - `Topic = {title, angle, ep_adjacent: string[], source_urls: string[], if_like_a_try_b, visual_style, why_fresh, revisit_candidate: boolean}`.
- `POST /api/thailandnow/scout/fireside/edit-notes` — body `{draft?: string, url?: string, check_coverage?: boolean}` → `{notes: Notes, mode: "direct"|"degraded", error?: string}`.
  - `Notes = {overall, strengths: string[], fixes: {anchor, note, severity: "must"|"should"|"nit"}[], structure_notes, voice_notes, coverage_check}`.

## What to build (frontend only — `ThailandNowPanel.tsx`)
1. **Widen the mode union** (L1655): `"pitch" | "image"` → `"pitch" | "image" | "fireside"`.
2. **Add a FIRESIDE MODE toggle button** alongside PITCH/IMAGE (L1813–1822 region), same `btn btn--compact` + `btn--signal`-when-active pattern.
3. **Add the fireside render branch** (by the `{scoutMode === "pitch" ? … : (image)}` at L1827/1971): a `<FiresidePanel />` (inline or small component in the same file).
4. **Inside FiresidePanel — a secondary toggle** `usePersistentState<"fireside-source"|"fireside-edit">("tn.scout.fireside.sub", "fireside-source")` with two compact buttons: **SOURCE TOPICS** / **EDIT NOTES**.
5. **SOURCE TOPICS sub-view** (mirror the existing PITCH async pattern: `scoutJobId` state + `usePolling("/api/thailandnow/jobs", 2000)` watching for the `fireside-source` job to flip to `done`, then GET the report):
   - Inputs: `seed` (text) + `category` (text/select) → SEARCH button → `POST …/fireside/source {seed, category}` → store job id.
   - While running: status line. On done: render topic cards, each showing **title**, **angle** (the "two questions"), **ep_adjacent** chips, **source_urls** (links), **if_like_a_try_b**, **visual_style**, **why_fresh**, a **revisit_candidate** badge when true, and a **mode** badge (`notebook` vs `web-fallback`) on the result set. COPY button per card.
6. **EDIT NOTES sub-view** (mirror the synchronous `scout_pitch` / MAKE-PITCH shape):
   - A `<textarea>` for the draft (or a URL input), + a `check_coverage` checkbox → NOTES button → `POST …/fireside/edit-notes`.
   - Render the notes block: **overall**, **strengths** (bulleted), **fixes** (each: quoted `anchor` + `note` + a severity-colored tag must/should/nit), **structure_notes**, **voice_notes**, **coverage_check** (if present). Show `error` + `mode:"degraded"` plainly when returned. COPY button.

## Reuse (do not invent new primitives)
`fetchJSON`, `usePolling` (from `../api`), `usePersistentState`, the existing card / expansion / COPY-button / `btn` classes, and the PITCH-mode async-job poll as the SOURCE template. Match the file's existing styling conventions exactly.

## Hard constraints
- **TypeScript-clean + builds.** No `any` where a real type fits (use the `Topic`/`Notes` shapes above). No new dependencies.
- Purely additive — do not refactor PITCH/IMAGE mode code.
- Persist user state via `usePersistentState` (keys prefixed `tn.scout.fireside.*`).

## Verification gate (agy runs; host re-checks)
```
cd "/var/home/NAZ/Coding Projects/Railjack/frontend" && npm run build
```
(`tsc && vite build` — must succeed with zero TS errors.) Host will also eyeball the diff + run the live UI at `:8700` once the corpus notebook exists (T7).

## Out of scope
Backend (done). Live UI test (needs corpus notebook — host, post-auth). Somatic port (post home-green).
