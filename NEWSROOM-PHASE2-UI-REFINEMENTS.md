# NEWSROOM ▸ Phase 2 UI refinements (drive browser + button uniformity + script paragraphs)

Three frontend-focused tweaks to the Phase 2 SEND TO NL / SEND TO RADIO controls in
`frontend/src/components/NewsroomPanel.tsx` (plus one tiny backend addition for Radio).
Spawned from `~/Cephalon`; follow `AGENTS.md`. Context: `NEWSROOM-PHASE2-IDE-BRIEF.md`.

## 1. Drive browser — REUSE the NEWS FILL picker (do NOT rebuild)

There is already a folder browser: `loadBrowse(folderId)` at `NewsroomPanel.tsx:316` calls
`GET /api/newsroom/radio/news/browse?parent=<id>` (defined `app/radio_news.py:97`) which
returns `{parent, folders, docs}` — a one-level Drive tree walk. Reuse it.

- Add a folder picker beside **SEND TO NL** rooted at the NL home folder
  `0BxI14z7NX9CIc3VJNGJwTGlJcG8`, and beside **SEND TO RADIO** rooted at the Radio home
  folder `1LSw5NwDhwg7PE9pJUO6jKPcd3yFBCOI9` (= radio.py `PARENT_FOLDER`).
- Drilling folders → picking a doc sets a `doc_id`. That `doc_id` flows into the fill:
  - **NL:** pass `doc_id` to `POST /api/newsroom/fill` (already supported — `app/newsroom.py:157`).
  - **Radio:** add a `--doc` alternative to `radio.py fill` (skip `find_day_doc` when `--doc`
    is given) and accept `doc_id` in `POST /api/newsroom/radio/fill` as an alternative to
    `year/month/day`. `section/block/slot` still required either way.
- **Default (no doc picked):** keep today's auto-resolve (NL `--today`, Radio `year/month/day`)
  so nothing regresses. The picker is additive.
- The browse endpoint's token is railjack RW (full-drive) — it can read **both** home folders;
  no new auth. (Confirm the NL home is browsable; if not, the newsroom token path needs access.)

## 2. Button uniformity

Today SEND TO NL is `btn btn--signal` and SEND TO RADIO is plain `btn` with inline color —
asymmetric. Make them **uniform**: both compact, matching the QUEUE/LEDGER/RADIO tab-button
size (`btn btn--compact`), identical style, with slightly clearer text labels. Drop the
asymmetric `btn--signal` / inline border-color. The TAB/SLOT and SECTION/BLOCK/SLOT selector
clusters should read as two parallel rows.

## 3. Script textarea must show paragraphs

The "Script (edit before sending)" `<textarea>` (`NewsroomPanel.tsx:801`) renders Ben's output,
which rule 6 mandates as **2–4 paragraphs**. Right now it can appear as one wall of text.

- Trace `rewritten` → load-into-Script → `sendText` and **do not strip `\n`** anywhere in that
  path. Paragraph breaks must survive into the textarea.
- Add `whiteSpace: "pre-wrap"` to the textarea style (defensive — guarantees `\n` renders).
- If Ben still returns a single block, strengthen the OUTPUT OVERRIDE in
  `app/newsroom.py::api_rewrite` to explicitly require "separate paragraphs with `\n\n`".

## Landing bar
- `npm run build` clean. On `:8700`: NL + Radio each show a folder picker rooted at their home
  folder; a picked doc fills correctly; both SEND buttons uniform/compact; Script textarea shows
  paragraphs.
- Restart railjack after any backend edit (`radio.py fill --doc`, `api_rewrite` override).
- Vault scripts (`radio.py`) → vault path FIRST, then byte-identical `~/.claude/skills/newsroom/scripts/`.
- COMMIT only — do not push Railjack. Do not change the fill slot-resolution logic (it's verified).

## Do not
- Do NOT rebuild the browse endpoint or folder-picker — reuse `loadBrowse` + `/api/newsroom/radio/news/browse`.
- Do not touch REWRITE/SEO/queue, the RADIO monthly generate, or the verified slot-fill matchers.
- No transliteration; markers only.
