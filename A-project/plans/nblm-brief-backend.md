# Task: Railjack NotebookLM module — BACKEND

Implement the backend of the NotebookLM ("RESEARCH") dashboard module in this
repo (Railjack, FastAPI). The audited plan is at
`/home/NAZ/.claude/plans/nested-wiggling-yeti.md` — read it if reachable; if
sandboxed out, THIS brief is the complete contract.

Study and mirror `app/comfyui.py` closely (job store `_JOBS`/`_BG`/deque
logs/cancel, `_under` path confinement, module-options lookup, argv lists via
`asyncio.create_subprocess_exec` — NEVER shell=True). Also read
`app/ffmpeg_jobs.py`, `app/config.py`, `tests/test_comfyui.py`,
`tests/conftest.py`.

The work drives the `notebooklm` CLI (v0.3.3, on PATH at
`~/.local/bin/notebooklm`). Its command surface (from the skill doc — trust
this, do not invent flags):
- `notebooklm auth check --json` · `notebooklm list --json`
- `notebooklm create "Title" --json` → `{id, title}`
- `notebooklm notebook delete <id>` (destructive)
- `notebooklm source list --json --notebook <id>`
- `notebooklm source add <url-or-path> --json --notebook <id>` → `{source_id, title, status}`
- `notebooklm source wait <source_id> -n <id> --timeout 120`
- `notebooklm source add-research "query" --mode fast|deep --no-wait --notebook <id>`
- `notebooklm research wait -n <id> --import-all --timeout 300`
- `notebooklm ask "question" --json --notebook <id>` (+ `-c <conversation_id>` for follow-ups) → `{answer, conversation_id, turn_number, references:[{source_id, citation_number, cited_text}]}`
- `notebooklm generate <type> [instructions] --json --notebook <id>` → `{task_id, status}`; types+options in the catalog below
- `notebooklm artifact list --json --notebook <id>`
- `notebooklm artifact wait <artifact_id> -n <id> --timeout 600` (exit 2 = timeout)
- `notebooklm download <type> <dest-path> -a <artifact_id> -n <id>`

IMPORTANT: always pass explicit `--notebook <id>` / `-n <id>`; NEVER use
`notebooklm use` (shared context file, parallel-unsafe).

## Deliverables
1. **NEW `app/notebooklm.py`** — one APIRouter mounted in `app/main.py`
   (1 import + 1 include_router — the only main.py change).
2. **NEW `app/zai.py`** — extract the z.ai call from `app/comfyui.py::expand`
   into `async def zai_message(prompt: str, max_tokens: int = 400) -> str`
   (raises HTTPException(503) if ZAI_API_KEY unset; same headers/model
   "glm-5"/endpoint as the existing code). **EDIT `app/comfyui.py`** to use it
   (expand shrinks; behavior identical — don't change its endpoint contract).
3. **NEW `tests/test_notebooklm.py`** — pytest, all subprocess/httpx mocked
   (must pass with no CLI/auth): argv construction for each endpoint,
   delete-confirm gate (mismatch → 400, match → runs delete argv), catalog
   shape (every type has option groups list), generate-409 (second generate
   while one queued/running), download path confinement (403 outside
   output_dir), job to_dict shape, alphabetical notebook sort.
4. **EDIT `configs/tawhan.yaml`** — append module:
   ```yaml
     - id: notebooklm
       title: RESEARCH
       kind: panel
       panel: notebooklm
       options:
         output_dir: "~/Downloads/NotebookLM Outputs"
         browse_root: "~"
   ```
   (no manage:, no health: — cloud service, panel handles auth state).

## API contract (frontend is built in parallel against EXACTLY this)
All under `/api/nblm/`. Options come from the `notebooklm` panel module's
`options` dict (pattern: `_comfyui_options()`).

- `GET /api/nblm/state` → `{available: bool, reason: str|null, authed: bool, account: str|null}`
  - available = `shutil.which("notebooklm")` non-null AND options block non-empty (module registered). Compute availability at import like comfyui `_AVAILABLE`.
  - authed/account via `notebooklm auth check --json` run async, **cached 60 s** (module-level `(expires, result)` tuple) — it hits Google. On CLI error → authed:false.
- `GET /api/nblm/notebooks` (query `refresh: bool = False`) → `{notebooks: [{id, title, ...passthrough}]}` — `notebooklm list --json`, **sorted case-insensitively by title**, cached in-process; `refresh=true`, create, and delete invalidate the cache. If the CLI errors (e.g. auth expired) → 502 with the CLI's message.
- `POST /api/nblm/notebooks` `{title}` → create --json passthrough.
- `POST /api/nblm/notebooks/{nid}/delete` `{confirm_title}` → 400 unless confirm_title == the notebook's actual title (look it up in the cached list, refresh if miss); then run `notebook delete <nid>`. (POST not DELETE verb — body needed.)
- `GET /api/nblm/notebooks/{nid}/sources` → `source list --json` passthrough `{sources: [...]}`.
- `POST /api/nblm/notebooks/{nid}/sources` `{url}` OR `{path}` → **job** (kind "source"). Path variant: expanduser/resolve + confine under options.browse_root (`_under`), 400 outside. Job runs `source add … --json`, parses source_id, then `source wait <sid> -n <nid> --timeout 120`; logs stream to job.logs.
- `POST /api/nblm/notebooks/{nid}/research` `{query, mode}` (mode ∈ fast|deep, default fast) → **job** (kind "research"): `source add-research <query> --mode <mode> --no-wait --notebook <nid>` then `research wait -n <nid> --import-all --timeout 300`.
- `POST /api/nblm/notebooks/{nid}/ask` `{question, conversation_id?}` → sync (no job): `ask --json` (+ `-c` when conversation_id given), 60 s timeout, passthrough `{answer, conversation_id, turn_number, references}`. CLI error → 502.
- `GET /api/nblm/catalog` → `{types: [...]}` — STATIC dict in the module, transcribed exactly from the skill's Generation Types table:
  ```
  audio:       formats [deep-dive, brief, critique, debate], lengths [short, default, long], ext mp3
  video:       formats [explainer, brief], styles [auto, classic, whiteboard, kawaii, anime, watercolor, retro-print, heritage, paper-craft], ext mp4
  slide-deck:  formats [detailed, presenter], lengths [default, short], ext pdf
  infographic: orientations [landscape, portrait, square], details [concise, standard, detailed], ext png
  report:      formats [briefing-doc, study-guide, blog-post, custom], ext md
  mind-map:    (no options), ext json
  data-table:  (description required — instructions field), ext csv
  quiz:        difficulties [easy, medium, hard], quantities [fewer, standard, more], ext json
  flashcards:  difficulties [easy, medium, hard], quantities [fewer, standard, more], ext json
  ```
  Shape: `{id, label, ext, groups: [{flag: "--format", label: "FORMAT", values: [...]}, ...], needs_instructions: bool}` (data-table needs_instructions true; instructions optional elsewhere).
- `POST /api/nblm/notebooks/{nid}/generate` `{type, options: {flag: value}, instructions?}` → **job** (kind "generate"), 409 if a generate job queued/running (source/research jobs don't block). Validate type against catalog and each flag/value against that type's groups (400 otherwise). Argv: `notebooklm generate <type> [instructions] --json --notebook <nid> <flag> <value>...`. Parse task/artifact id from --json output, then `artifact wait <aid> -n <nid> --timeout 3600`, then auto-download: dest = `output_dir/<slug(notebook title)>/<type>-<aid[:8]>.<ext>` (mkdir parents), `download <type> <dest> -a <aid> -n <nid>`; set job.output_paths=[dest]. Progress: 5 after queue, 10 after id parsed, 90 after wait, 100 done (no real % available).
- `GET /api/nblm/notebooks/{nid}/artifacts` → `artifact list --json` passthrough `{artifacts: [...]}`.
- `POST /api/nblm/artifacts/download` `{notebook_id, artifact_id, type}` → **job** (kind "download"), same dest scheme + download argv as above (for artifacts made in the web UI).
- `GET /api/nblm/outputs` (query `notebook: str|None` = title-slug subfolder filter) → `{files: [{name, path, mtime, ext}]}` newest first, recursive under output_dir, exts {mp3,mp4,png,pdf,md,json,csv,pptx,html}.
- `GET /api/nblm/outputs/file?path=` → FileResponse, confined under output_dir (copy comfyui's outputs_file verbatim incl. 403/404).
- `POST /api/nblm/polish` `{draft, purpose}` (purpose ∈ chat|generate) → `{polished}` via `zai_message()`. Prompt: rewrite the draft into a clear, specific NotebookLM <chat question | generation instruction>, keep the same language as the draft, return only the rewritten text.
- `GET /api/nblm/jobs` → `{jobs: [{id, kind, label, status, progress, output_paths, error, logs}]}` newest first · `POST /api/nblm/jobs/{jid}/cancel` — SIGTERM the running subprocess (jobs hold `proc` like comfyui).

Job runner detail: each job may run SEVERAL sequential CLI subprocesses; store the current `proc` on the job so cancel kills the active one and the runner checks `job.cancel` between steps.

## Rules
- Mirror repo idioms; ponytail (shortest working code, no speculative abstractions, match comment style).
- httpx only (via app/zai.py); no new Python dependencies.
- Do NOT touch frontend/ — another agent owns it.
- Do NOT commit.

## Verify before finishing (must pass)
```bash
cd "/var/home/NAZ/Coding Projects/Railjack"
.venv/bin/python -c "from app.main import app"
.venv/bin/python -m pytest -q     # FULL suite
```
If the permission gate blocks these commands, say so explicitly in your report.
Report: files created/changed, test count, any contract deviations.
