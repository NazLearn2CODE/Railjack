# NEWSROOM ▸ Queue REWRITE — SLICE B (frontend)

Second slice. Build **only after Slice A lands** and freezes the contract below. Full
context in `NEWSROOM-REWRITE-BEN-IDE-BRIEF.md`. You depend ONLY on the API shape below
(already frozen by Slice A) — do not touch backend or vault files. Follow `AGENTS.md`.

## Scope (this slice ONLY)
- `frontend/src/components/NewsroomPanel.tsx`

Reuse its existing `fetchJSON` / `post` / `error` helpers and phosphor/HUD styling.

## Frozen contract (from Slice A — do not change it)
`POST /api/newsroom/rewrite` body `{text: str}` → `200 {"rewritten": str, "seo": str}`.
- `rewritten`: broadcast prose with **literal `**name**` markers** around person names.
- `seo`: AI SEO Block (Version A + B) text.

## Build
1. `rewrite()` (~L562): read `{rewritten, seo}` from the response; store both —
   `setRewritten(d.rewritten)`, `setSeo(d.seo || "")`.
2. Preview iframe `rewriteDoc` (~L20): render `**name**` → `<strong>name</strong>`
   (HTML-escape everything else). **Markers stay in the raw `rewritten` string** so SEND TO NL
   can convert them. Below the prose, render the `seo` block (Version A + B) as a **copyable**
   region.
3. "load into Script" (~L742): load `rewritten` **with markers intact** into the Script textarea.
4. `sendToNL()` (~L545): **UNCHANGED wire** — still posts `{today:true, text:sendText}` to
   `/api/newsroom/append`. The markers + dates become Doc formatting via Slice A's nl_append
   change. **Do NOT append the SEO block** to the NL Doc — it is panel-only / copyable.

## Landing bar
- `npm run build` (tsc + vite) → clean.
- On `:8700` → NEWSROOM → **queue** → REWRITE: preview shows **bold** person names + the A+B
  SEO block; "load into Script" keeps the `**name**` markers; SEND TO NL writes the script to
  today's NL Doc with names bold + dates underlined (via Slice A).

## Do not
- No backend or vault edits. Do not append SEO to the Doc. Do not touch the RADIO sub-tab. Do
  not build position-targeted insertion or a Radio-doc button (phase 2).
