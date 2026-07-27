# RADIO ▸ News Fill — Antigravity IDE brief (frontend only)

Build the **frontend half** of RADIO ▸ News Fill. Backend is being built separately against the same
contract; you depend ONLY on the API shapes below (already frozen). Do not touch backend files.

## Read first
- `RADIO-NEWS-BRIEF.md` → section **"Task B — FRONTEND"** is your authoritative spec.
- `frontend/src/components/NewsroomPanel.tsx` → the file you edit. Reuse its existing
  `fetchJSON`/`post`/`error` helpers and phosphor/HUD styling. The RADIO tab already exists
  (Document Generator) — you are adding a second mode, NOT replacing it.

## What to build (inside the `{tab === "radio" && (...)}` block)
Add a **mode toggle** at the top: **Document Generator** (existing UI, unchanged) | **News Fill** (new).

News Fill mode, top-to-bottom:
0. **Working document** dropdown — `GET /api/newsroom/radio/news/docs`
   → `{docs:[{id,name,link,kind,date}]}`. Show newest first (list is already date-desc). Store the
   chosen `{id, kind}`.
1. **Category** dropdown `global | business` + **SCOUT** button
   → `POST /api/terminal/insert` body `{text: "/radio-news-scout " + category}`
   (types the slash-command into the tmux pane; Naz presses Enter himself — do NOT auto-submit).
   Then a **CONVERT** button → `GET /api/newsroom/radio/news/report`
   → `{category, results:[{title,url,source,date,content,words}], count, slice_of_life:[…], mtime}`.
2. Render `results` as a **numbered checklist** — reuse a `selected` record keyed by `url`
   (like the Story-Scout image basket). Show the **target hard count** for (category, kind):
   weekday global **10** · weekday business **10** · weekend global **7** · weekend business **6**.
   On a **weekday + global** doc, also render a **Slice of Life** section listing `slice_of_life`
   with single-pick **radios** (pick exactly 1).
3. **CONFIRM** button — enabled ONLY when the hard target is met (and, if weekday-global, one Slice
   picked) → `POST /api/newsroom/radio/news/apply`
   body `{doc_id, kind, category, pieces:[…selected in rank order…], slice:{…}|null}`
   → `{doc_id, category, written:[{tab,slot,title}], skipped:[{tab,slot,reason}]}`.
   Render `written` (tab · slot · title) and any `skipped`.

Surface backend `_fatal`→400 detail via the shared `error` state, like the other calls.

## kind → target-count map (hard picks; the Slice pick is separate, weekday-global only)
weekday global 10 · weekday business 10 · weekend global 7 · weekend business 6

## Landing bar
`npm run build` (tsc + vite) passes. On `:8700` NEWSROOM → RADIO → News Fill: pick a doc, SCOUT types
the command into the terminal, CONVERT lists results, CONFIRM stays disabled until the target is met.

## Do not
- Do not modify the Document Generator UI/logic.
- Do not re-implement `/api/terminal/insert` — call it as-is.
- Do not build against imagined backend fields — only the shapes above.
