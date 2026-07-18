# Railjack — NotebookLM research module (v1)

## Context
Naz wants a research module in the Railjack hub driving Google NotebookLM
(just rebranded **Gemini Notebook**, 2026-07-16) through the `notebooklm` CLI
that backs the `/notebooklm` skill. Probe results (2026-07-19):

- **CLI installed** (`notebooklm` 0.3.3, `~/.local/bin`), skill doc matches it.
- **Auth EXPIRED** — `notebooklm list` fails with a Google sign-in redirect.
  `notebooklm login` is an interactive browser flow → **Naz must run it once**
  before live verification (panel gets a LOGIN REQUIRED state for this).
- **Rebrand impact: none yet.** notebooklm-py upstream is alive (v0.7.3 on the
  0.7.x line, 0.8.0 betas after the rebrand), README notes the rebrand, and
  an open issue merely *monitors* for a future domain migration. Service still
  answers at notebooklm.google.com. **Stay on pinned 0.3.3** (the skill doc
  documents its exact flags); upgrade only if the smoke test fails post-login.
- CLI is scriptable: `--json` on all list/create/add/ask/generate commands,
  explicit `-n <notebook_id>` for parallel-safe targeting, `download` writes
  server-side files. Long ops (generate 5–45 min, deep research 2–5 min,
  source indexing 10–60 s) → the ffmpeg/comfyui job-store pattern fits.

User decisions: polish = **z.ai direct** (like ComfyUI AI-EXPAND) · previews =
**rich** (inline audio/video/png/markdown) · scope = **full research loop**
(defer sharing/language/slide-revision) · delete = **with type-the-title
confirm gate**.

Not GPU-bound → works for Tasai too once she runs `notebooklm login` on
Orokin; gate is CLI-on-PATH + auth, not hardware.

## Approach

### Backend — NEW `app/notebooklm.py` (router mounted in `app/main.py`)
Shells out to the `notebooklm` CLI (argv lists via
`asyncio.create_subprocess_exec`, never shell). Mirrors `app/comfyui.py`
job-store (`_JOBS`/`_BG`/deque logs/cancel via SIGTERM). Options from the
module's `options:` dict (`_comfyui_options()` pattern). Every CLI call gets
explicit `--notebook`/`-n` — never `use` (shared-context footgun).

Endpoints (all under `/api/nblm/`):
- `GET state` — `{available, reason, authed, account}`. available = CLI on
  PATH + options block present. authed via `notebooklm auth check --json`
  (cached 60 s — it hits Google). Not authed → frontend shows LOGIN REQUIRED
  + a button that inserts `notebooklm login` into the terminal via the
  existing `POST /api/terminal/insert`, then flips to the tmux module.
- `GET notebooks?refresh=` — `notebooklm list --json`, cached in-process
  (invalidate on create/delete/refresh). Sorted alphabetically server-side;
  search filtering is client-side (list is small).
- `POST notebooks {title}` / `DELETE notebooks/{id} {confirm_title}` —
  create / delete; delete 400s unless `confirm_title` matches the notebook's
  actual title (the panel's type-the-name gate, enforced server-side).
- `GET notebooks/{id}/sources` — `source list --json`.
- `POST notebooks/{id}/sources {url}` or `{path}` — URL or server-path add
  (path confined under browse root ~, reuse `_under`). Job (indexing takes
  time; run `source add --json` then the job polls `source list` until READY).
- `POST notebooks/{id}/research {query, mode: fast|deep}` — job wrapping
  `source add-research … --no-wait` + `research wait --import-all`.
- `POST notebooks/{id}/ask {question, conversation_id?}` — `ask --json`,
  returns `{answer, references, conversation_id}`. Sync (seconds).
- `GET catalog` — static artifact-type catalog: the 10 generate types with
  their exact `--format/--style/--length/--difficulty/--quantity/--orientation/--detail`
  option lists transcribed from the skill's Generation Types table (backend-
  served so the UI dropdowns grow without frontend edits — same philosophy as
  `/api/ffmpeg/ops`).
- `POST notebooks/{id}/generate {type, options{}, instructions?}` — job:
  `generate <type> … --json` → parse task/artifact id → poll
  `artifact list --json` until completed → **auto-download** into
  `options.output_dir/<notebook-title-slug>/` with the CLI's `download <type>
  -a <id> -n <id>` → job.output_paths. One generate job at a time (409),
  research/source jobs run free.
- `GET notebooks/{id}/artifacts` — `artifact list --json` (for the artifact
  card; includes ones made in the web UI).
- `POST artifacts/download {notebook_id, artifact_id, type}` — job, same
  download path as above (for artifacts that already existed).
- `GET outputs?notebook=` + `GET outputs/file?path=` — list + serve files
  under `output_dir` (copy the comfyui outputs/file confinement verbatim;
  FileResponse streams mp3/mp4/png/pdf/md fine).
- `POST polish {draft, purpose}` — z.ai GLM call. **Refactor**: extract
  `_zai_message(prompt, max_tokens) -> str` from `app/comfyui.py::expand`
  into a small shared `app/zai.py`; both routers use it. Prompt: tighten the
  user's draft into a clear NotebookLM chat/generation instruction, same
  language as input.
- `GET jobs` / `POST jobs/{id}/cancel` — same shapes as comfyui.

### Data — none beyond the static catalog dict in `app/notebooklm.py`
(No YAML: unlike model URLs it never varies per machine, and the option enums
change only with CLI upgrades. One dict, served by `GET catalog`.)

### Frontend — NEW `frontend/src/components/NotebookPanel.tsx`
Registered `notebooklm: NotebookPanel` in the `App.tsx` PANELS map. Mirrors
ComfyPanel idioms (usePolling, fetchJSON, hud cards, reveal stagger, Pip,
localStorage `nblm.*`). Two-column inside the panel: left = notebook rail,
right = the workbench for the selected notebook.

1. **Header card** — "RESEARCH DECK" title + auth pip. Not authed → LOGIN
   REQUIRED card with the terminal-insert button (no other cards).
2. **Notebook rail (left, w-64)** — search `input` (client-side filter),
   alphabetical list (backend pre-sorted), active = `btn--signal`, NEW
   NOTEBOOK button + inline title input. Selected id in localStorage.
3. **Sources card** — source list w/ status pips; ADD URL input; ADD FILE
   (server-path input; ~ paths allowed). Research row: query input +
   FAST/DEEP toggle + RESEARCH button (job).
4. **Chat card** — question textarea; **POLISH** button (→ `/polish`,
   replaces draft, caret spinner — the "polish with the current model" ask);
   ASK button; answer rendered with `[n]` citation chips (hover shows
   `cited_text`, from the ask response's references); conversation continues
   via returned `conversation_id`; SAVE topic note deferred to v2.
5. **Generate card** — TYPE `<select>` from `/catalog`, then one `<select>`
   per option group of the chosen type (dropdowns of ALL skill options, by
   category — mirrors the VDO-Lab backend-driven-menu approach), optional
   instructions textarea (POLISH works here too), GENERATE (jobbed; note in
   UI: "5–45 min").
6. **Artifacts + outputs card** — artifact list (status, type, DOWNLOAD for
   not-yet-downloaded); downloaded files grid: mp3 → `<audio controls>`,
   mp4 → `<video controls>`, png → `<img>`, md → fetched + rendered in the
   existing `.prose-md` styles via **`marked`** (one tiny new dep — the only
   sane way to render reports; `.prose-md` CSS already exists unused), other
   (pdf/pptx/csv/json) → download link.
7. **Jobs card** — identical to ComfyPanel's (poll 1 s, progress, log tail,
   cancel).
8. **Danger row** (bottom of notebook rail) — DELETE NOTEBOOK: reveals a
   type-the-title input; button stays disabled until it matches; server
   re-verifies.

### Config — `configs/tawhan.yaml`
New module (no `manage:`, no `health:` — cloud service; the auth pip in-panel
is the health):
```yaml
  - id: notebooklm
    title: RESEARCH
    kind: panel
    panel: notebooklm
    options:
      output_dir: "~/Downloads/NotebookLM Outputs"
      browse_root: "~"
```
Orokin/Somatic: Tasai adds the same block on her channel when ready; absent
block → module absent, endpoints `available:false`.

## Files
- NEW `app/notebooklm.py` — router + CLI wrapper + job store + catalog dict.
- NEW `app/zai.py` — shared `_zai_message()` (extracted from comfyui.expand).
- NEW `frontend/src/components/NotebookPanel.tsx`.
- NEW `tests/test_notebooklm.py` — CLI-argv construction, delete-confirm gate,
  catalog shape, job serialization, path confinement, generate-409. All
  subprocess/httpx mocked (tests pass with no auth/CLI).
- EDIT `app/main.py` — mount router (2 lines).
- EDIT `app/comfyui.py` — use `app/zai.py` helper (shrinks expand).
- EDIT `frontend/src/App.tsx` — PANELS entry (2 lines).
- EDIT `frontend/package.json` — add `marked`.
- EDIT `configs/tawhan.yaml` — module block above.

## Reuse
- `notebooklm` CLI 0.3.3 (pinned; matches skill doc) — all heavy lifting.
- `app/comfyui.py` — job store, `_under` confinement, outputs/file endpoints
  (copy), z.ai call (extracted to `app/zai.py`).
- `POST /api/terminal/insert` — the login hand-off + any "send to Tawhan".
- `.prose-md` CSS (already in index.css, currently unused) for report preview.
- ComfyPanel/FfmpegPanel component idioms throughout.

## Delegation (agent-x, after plan approval)
Same split as ComfyUI: backend brief + frontend brief to two GLM-5 children
in parallel with the exact `/api/nblm/*` contract frozen in both briefs;
Fable host verifies (pytest, npm build, live smoke) before commit.

## Verification
1. `.venv/bin/python -m pytest -q` — full suite green (new tests mocked).
2. `cd frontend && npm run build` — strict TS clean.
3. **Naz runs `notebooklm login`** (interactive browser — only human step).
4. Live: restart hub → RESEARCH module appears; notebooks list alphabetical +
   search filters; create scratch notebook; add one URL source; ask one
   question (citations render); POLISH round-trips via GLM; generate a quiz
   (fastest artifact) → auto-download → renders/downloads in the panel;
   delete scratch notebook via the type-title gate.
5. Gate: `RAILJACK_CONFIG=orbiter-grimoldi` TestClient → `available:false`,
   module absent from `/api/config`.

## Deferred to v2
Sharing management, language switching, slide revision, source fulltext
viewer, save-chat-as-note, notebook rename, drive-mode research, upload from
the panel's own file-picker (browser upload → server temp), artifact delete.
