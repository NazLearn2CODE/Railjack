# Task: Railjack NotebookLM module — FRONTEND

Implement the frontend of the NotebookLM ("RESEARCH") dashboard module
(React 19 + Vite + Tailwind v4, dark HUD "Orbiter" theme, strict TS).

Study and mirror `frontend/src/components/ComfyPanel.tsx` (newest panel — its
idioms are canonical: usePolling/fetchJSON from `../api`, local Pip, hud
cards, reveal stagger, job rows, localStorage helpers, err state) plus
`FfmpegPanel.tsx`, `App.tsx`, `store.ts`, `api.ts`, `index.css` (note
`.prose-md` styles — already defined, currently unused; you will use them).

## Deliverables
1. **NEW `frontend/src/components/NotebookPanel.tsx`**
2. **EDIT `frontend/src/App.tsx`** — 1 import + `notebooklm: NotebookPanel` in PANELS.
3. **EDIT `frontend/package.json`** — add dependency `"marked": "^16.0.0"`
   then run `npm install` (lockfile update is expected and wanted).
   Use it: `import { marked } from "marked"` →
   `<div className="prose-md" dangerouslySetInnerHTML={{ __html: marked.parse(mdText) as string }} />`.

## Backend API (built in parallel — code against EXACTLY this; don't probe the live server for shapes)
All under `/api/nblm/`:
- `GET state` → `{available, reason, authed, account}` (poll 30s)
- `GET notebooks?refresh=true|false` → `{notebooks: [{id, title}]}` (already alphabetical)
- `POST notebooks {title}` → created notebook
- `POST notebooks/{id}/delete {confirm_title}` (400 if title mismatch)
- `GET notebooks/{id}/sources` → `{sources: [{source_id, title, status}]}`
- `POST notebooks/{id}/sources {url}` or `{path}` → job
- `POST notebooks/{id}/research {query, mode: "fast"|"deep"}` → job
- `POST notebooks/{id}/ask {question, conversation_id?}` → `{answer, conversation_id, turn_number, references: [{source_id, citation_number, cited_text}]}` (sync, may take ~30s — show a caret spinner)
- `GET catalog` → `{types: [{id, label, ext, groups: [{flag, label, values: [string]}], needs_instructions}]}`
- `POST notebooks/{id}/generate {type, options: {"--format": "brief", ...}, instructions?}` → `{id}` job (409 = one already running; generation takes 5–45 min, tell the user in the UI)
- `GET notebooks/{id}/artifacts` → `{artifacts: [...]}` (passthrough; render defensively: show `type`/`title`/`status`-ish fields if present, else JSON-ish fallback line)
- `POST artifacts/download {notebook_id, artifact_id, type}` → job
- `GET outputs?notebook=<title-slug>` → `{files: [{name, path, mtime, ext}]}`; file src = `/api/nblm/outputs/file?path=<encodeURIComponent(path)>`
- `POST polish {draft, purpose: "chat"|"generate"}` → `{polished}`
- `GET jobs` → `{jobs: [{id, kind, label, status, progress, output_paths, error, logs}]}` (poll 1s) · `POST jobs/{jid}/cancel`
- Existing endpoint reused: `POST /api/terminal/insert {text}` (400 on newlines — single line only).

## Layout
Panel component signature `{ module }: { module: ModuleConfig }` (registered for module id `notebooklm`, title RESEARCH).

Top-level: if `state.available === false` → single centered card "RESEARCH NOT AVAILABLE ON THIS MACHINE" + reason (copy ComfyPanel's pattern). Else if `state.authed === false` → single centered card "LOGIN REQUIRED": explain `notebooklm login` must run in the terminal, one `btn btn--signal` "INSERT LOGIN COMMAND IN TERMINAL" that POSTs `{text: "notebooklm login"}` to `/api/terminal/insert` then switches to the tmux module (copy ComfyPanel's sendToTawhan module-switch). Else the workbench:

**Two-column: left rail (w-64 shrink-0) + right workbench (flex-1, scrollable column of hud cards).**

LEFT RAIL (hud hud--bracket, full height, flex-col):
- panel-title "RESEARCH DECK" + auth pip (pip--go) + `state.account` (label, truncated).
- search `input` (placeholder "search notebooks") — client-side case-insensitive title filter.
- notebook buttons list (scrollable flex-1): `btn hud--bracket` style like ModuleRail's module buttons, active = `btn--signal`. Selected id persisted `localStorage nblm.notebook`.
- NEW NOTEBOOK: button toggles an inline title `input` + CREATE btn → POST, refresh list, select it.
- refresh button (↻, calls `notebooks?refresh=true`).
- DANGER (mt-auto, bottom): DELETE NOTEBOOK btn (`btn--crit` hover) → reveals inline input placeholder "type notebook title to confirm" + CONFIRM DELETE btn disabled until input === selected notebook's title exactly; on success clear selection + refresh.

RIGHT WORKBENCH (cards, reveal stagger; all disabled/hidden until a notebook is selected — show "select a notebook" label otherwise):
1. **SOURCES card** — rows (pip by status: READY→go, processing→hazard, else muted; title). ADD URL: input + ADD btn. ADD PATH: input (placeholder "~/path/to/file.pdf") + ADD btn. RESEARCH row: query input + FAST/DEEP toggle pair (btn--signal on active) + RESEARCH btn → job. Refetch sources when a source/research job flips to done (watch jobs list).
2. **CHAT card** — scrollable exchange history (local state array of {q, answer, references}): question in `.mono` muted, answer rendered via marked in `.prose-md`, then citation chips: small bordered `[n]` spans, `title={cited_text}` tooltip. Textarea (rows 3) + POLISH btn (POST polish purpose:"chat", replaces textarea, caret while busy) + ASK btn (disabled while in flight, caret spinner). Keep `conversation_id` from the last answer and pass it on follow-ups; NEW TOPIC btn clears it + history.
3. **GENERATE card** — TYPE `<select className="input">` from catalog. Under it, one labeled `<select>` per `groups` entry of the chosen type (label = group.label; first value preselected). If `needs_instructions`, instructions textarea is required (disable GENERATE when empty); else optional. POLISH btn here too (purpose:"generate", polishes the instructions textarea). GENERATE btn `btn--signal` → POST generate; on 409 show error in the card. Muted `.label` note: "GENERATION TAKES 5–45 MIN — WATCH JOBS".
4. **ARTIFACTS card** — fetch artifacts when notebook selected + when a generate job completes. Rows: type/title/status + DOWNLOAD btn → POST artifacts/download (job).
5. **OUTPUT FEED card** — poll `outputs?notebook=<slug>` every 10s while a notebook is selected. slug = lowercase title, non-alphanumerics → "-", trimmed (match backend: assume simple slug; if a file 404s it just shows empty). Render by ext: mp3 → `<audio controls src=…>` full-width; mp4 → `<video controls>` (max-h-64); png → `<img>` grid; md → EXPAND/COLLAPSE row that fetches the file text once and renders via marked in `.prose-md`; pdf/pptx/csv/json → `<a>` download link row with name + mtime date.
6. **JOBS card** — copy ComfyPanel's jobs card verbatim (poll 1s, Pip, progress bar, log tail, CANCEL) pointing at `/api/nblm/jobs`.

## Rules
- Strict TS: `npm run build` must pass zero-error. `marked.parse` returns `string | Promise<string>` — cast `as string` (sync mode default).
- Only touch the three files listed (+ lockfile via npm install). Do NOT touch app/, configs/, other components, index.css.
- Ponytail: mirror existing idioms, no other new deps, shortest working code.
- Do NOT commit.

## Verify before finishing
```bash
cd "/var/home/NAZ/Coding Projects/Railjack/frontend" && npm install && npm run build
```
If the permission gate blocks it, say so explicitly. Report files changed + build result.
