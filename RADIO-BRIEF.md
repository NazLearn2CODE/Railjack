# RADIO — NEWSROOM sub-module build brief

Monthly Google-Drive batch generator. For a given **year + month**, copy three
templates into the pre-existing month folder inside a parent Drive folder:

- **1 spreadsheet** per month  — template `12Nv-r_IBk-ClRUGJf3TCp850Hswv9brsVF41UD_r77Q`
- **1 "Weekend Script" doc per Saturday & Sunday** — template `1insfe6ZIPyUINFd7g5DOmcIe-XCEfBb-PfpK9GQX8bo`
- **1 "Weekday Script" doc per Monday–Friday** — template `1QuFzKF8nuUUWahQuxgLwz1aU_5j9-LMbXuNBFVMBhyg`

Parent Drive folder: `1LSw5NwDhwg7PE9pJUO6jKPcd3yFBCOI9`.
Month folders **already exist**, named `YYYYMM MonthName` (e.g. `202608 August`).
RADIO **finds** the month folder by name (never creates it) and drops files in.
**Idempotent**: skip any file whose target name already exists in the folder.

This brief is the single source of truth for BOTH builders. The **Contract**
section is binding for both. Then Task A (backend, GLM-5) and Task B (frontend,
Antigravity IDE) each build their half against it.

---

## Contract (binding for both halves)

### `radio.py` CLI
```
python3 radio.py --year YYYY --month M [--sheet-name NAME] [--parent FOLDER_ID] [--dry-run]
```
- `--year`, `--month` required (month 1–12).
- `--sheet-name` optional; **default = the matched month folder's own name** (e.g. `202608 August`).
- `--parent` optional; default = the parent folder id above.
- `--dry-run` computes the plan and prints it **without any write call**.
- **Always prints one compact JSON object to stdout** (no `--json` flag needed).

### File naming (exact, no extension — Google files carry none)
- Weekend day → `YYYYMMDD_Weekend Script`  (e.g. `20260801_Weekend Script`)
- Weekday day → `YYYYMMDD_Weekday Script`  (e.g. `20260803_Weekday Script`)
- Spreadsheet → the sheet name (default folder name).
- Weekend = Saturday + Sunday (`date.weekday()` 5 or 6). Weekday = Mon–Fri (0–4).

### stdout JSON shape
Success (real run):
```json
{ "folder": {"id":"...","name":"202608 August"}, "dry_run": false,
  "counts": {"weekend":10,"weekday":21,"sheet":1,"planned":32,"to_create":32,"skipped":0},
  "created": [{"name":"202608 August","id":"...","link":"https://...","kind":"sheet"}, ...],
  "skipped": [] }
```
Dry run: same but `"dry_run": true`, `"created": []`, and a `"to_create": [{"name","kind"}, ...]` list.
Handled failure (folder missing, token missing, API error): print `{"_fatal": "message"}` and **exit 1**.
(This matches `app/newsroom.py` `_json`/`_fail`, which turn `_fatal` into a clean 400.)

### Auth
Reuse the **railjack read-write token** — `~/.config/railjack/google_token.json`
(scopes include full `drive`; confirmed). Refresh exactly like
`app/thailandnow.py:_google_token` (POST client_id/secret/refresh_token to `token_uri`).
Do **not** use the newsroom token (`~/.config/newsroom/google_token.json`) — it is `drive.readonly`.

### Drive REST calls (stdlib `urllib`, no MCP — mirror `nl_append.py`)
- **Find month folder:** `GET drive/v3/files?q=` with
  ``'{parent}' in parents and name contains '{YYYYMM}' and mimeType='application/vnd.google-apps.folder' and trashed=false``,
  `supportsAllDrives=true&includeItemsFromAllDrives=true`, `fields=files(id,name)`. Among matches, pick the
  one whose name **starts with** `YYYYMM`. None → `_fatal` "no month folder named like 'YYYYMM …' in parent".
- **Existing names (idempotency):** `GET drive/v3/files?q=` with ``'{folder}' in parents and trashed=false``,
  `fields=nextPageToken,files(name)`, paginate → a `set()` of names.
- **Copy:** `POST drive/v3/files/{templateId}/copy?supportsAllDrives=true&fields=id,name,webViewLink`
  body `{"name": name, "parents": ["{folderId}"]}`. Works for both Docs and Sheets.

### `build_plan(year, month, sheet_name)` — pure, no network (so it's unit-testable)
Returns an ordered list `[{template_id, name, kind}]`: the sheet first, then each day of the month
in order (`calendar.monthrange`), classified weekend/weekday. Keep this function network-free.

---

## Task A — BACKEND (delegated to GLM-5 via agent-x)

Match the house style: stdlib-only (`urllib`, `json`, `argparse`, `calendar`, `datetime`),
JSON-to-stdout, `{"_fatal": …}`+exit-1 for handled errors. Read `app/thailandnow.py:264-317`
(token + copy/create pattern) and `~/.claude/skills/newsroom/scripts/nl_append.py` (auth + `api()`
helper + `_fatal` convention) before writing — copy those idioms, don't invent new ones.

1. **`radio.py`** — write to the VAULT path first:
   `/home/NAZ/Cephalon/10-knowledge/skills/newsroom/scripts/radio.py`
   then copy it **byte-identical** to `/home/NAZ/.claude/skills/newsroom/scripts/radio.py`
   (the two newsroom skill dirs are kept identical; the Railjack backend shells out to the VAULT copy).
   Implement: `google_token()`, `find_month_folder()`, `existing_names()`, `copy_file()`,
   `build_plan()`, `main()` per the Contract. Add a module docstring like the other scripts.

2. **`app/newsroom.py`** — add two endpoints using the existing `_script` helper (do not add a new
   subprocess runner). Put a `RADIO = SCRIPTS / "radio.py"` next to `QUEUE`/`APPEND`:
   - `POST /api/newsroom/radio/preview` — body `{year:int, month:int, sheet_name?:str}`.
     400 if year/month missing. argv: `[PY, str(RADIO), "--year", str(year), "--month", str(month), "--dry-run"]`
     (+ `--sheet-name` if given). `return await _script(argv)`.
   - `POST /api/newsroom/radio/generate` — same body/argv **without** `--dry-run`; `timeout=180`
     (a full month is ~31 copy calls).
   No change needed in `app/main.py` (same router).

3. **`tests/test_newsroom.py`** — add RADIO tests matching the file's existing style:
   - `build_plan(2026, 8, "202608 August")`: first item is the sheet; assert a known Saturday
     (2026-08-01) → `20260801_Weekend Script` kind `weekend`, a known Monday (2026-08-03) →
     `20260803_Weekday Script` kind `weekday`; correct weekend/weekday counts for Aug 2026.
   - dry-run path makes **no** network calls (monkeypatch `google_token`/`copy_file` to explode if hit;
     stub `find_month_folder`/`existing_names`).

**Landing bar:** `pytest tests/test_newsroom.py` green, and
`python3 /home/NAZ/Cephalon/10-knowledge/skills/newsroom/scripts/radio.py --year 2026 --month 8 --dry-run`
prints valid JSON with the right counts. Do NOT do a live (non-dry-run) write — the host verifies that.

---

## Task B — FRONTEND (delegated to Antigravity IDE)

File: `frontend/src/components/NewsroomPanel.tsx`. Add a **RADIO** tab beside QUEUE/LEDGER.

- Extend the tab state: `"queue" | "ledger" | "radio"`; add a `RADIO` toggle button (same
  `btn btn--compact` style; `btn--signal` when active).
- RADIO panel (wrap in the same `hud hud--bracket reveal reveal-1` container as the other tabs):
  - **Inputs:** year (number, default current year), month (select 1–12 or number input),
    optional sheet-name text field.
  - **PREVIEW** button → `POST /api/newsroom/radio/preview` `{year, month, sheet_name?}`.
    On success render `counts`: e.g. `1 sheet · 21 weekday · 10 weekend · 32 to create · 0 skip`,
    plus the `to_create` list and the target `folder.name`.
  - **GENERATE** button — **disabled until a successful PREVIEW** (this is the confirm gate) →
    `POST /api/newsroom/radio/generate` same body. On success render the `created` list with
    `webViewLink` anchors (`link`) and the skipped count.
  - Reuse the existing `post`/`fetchJSON` helpers and the shared `error` state for failures
    (the backend returns `_fatal` messages as HTTP 400 detail — surface them like the other calls).
  - Keep the terminal/phosphor styling consistent with the rest of the panel.

**Landing bar:** `npm run build` (or the repo's typecheck/lint) passes; on `:8700` NEWSROOM → RADIO,
PREVIEW shows counts for a month, GENERATE stays disabled until a preview returns.

---

## Both: do not
- Do not create the month folder (find-only; `_fatal` if absent).
- Do not do a live write during the build — dry-run only. The host runs the first real generate.
- Do not touch queue/append/rewrite logic or the newsroom token.
