# BRIEF — Thailand NOW EVENTS: registry maintenance + pipeline tracking

Railjack (FastAPI hub + React cockpit). Target repo: `/var/home/NAZ/Coding Projects/Railjack`.
You are a single-turn builder: implement EXACTLY this brief, no extras, no formatting churn
outside touched regions. Follow existing code idioms in each file. When done, run nothing —
the host gates you.

## Context (what exists — read these anchors first)

- `app/thailandnow.py` (~5400 lines):
  - `_read_sheet_col` (~line 1929), `COVERED_OURS_SHEET` / `COVERED_COMPANY_SHEET` (~1919),
    `_covered_slug` (~1924), `_covered_events` (~1947), `GET /api/thailandnow/events/covered`
    (~1970). Google HTTP idiom: `token = await _google_token()`, inline `httpx.AsyncClient`,
    Bearer header, `HTTPException(502, ...)` on failure. `_google_token()` at ~307.
  - `POST /api/thailandnow/events/create` (~3081) → calls shared `provision(payload)` and
    returns its result: `{"desk_id","count","yyyymm","items":[{"nn","doc_name","doc_url","card_name","card_url"}]}`.
  - `POST /api/thailandnow/events/publish-from-card` (~5176, dual-registered for articles/blogs;
    `kind` variable is "event"|"article"|"blog"). WP draft creation succeeds at ~5369-5378;
    `post_id`, `permalink`, `link`, `title`, `slug` in scope; big `return {...}` at ~5380.
- `frontend/src/components/ThailandNowPanel.tsx` (~4600 lines), `EventsTab`:
  - `slugCovered` helper (~1186); covered fetch-on-mount (~1224-1234);
    `sorted` useMemo (~1283-1291); mode-toggle button row (~1526-1547, idiom
    `btn btn--md` / `btn--signal`); RESULTS header (~1771-1778, has CLEAR with
    `btn btn--compact btn--crit`); ✓ COVERED badge (~1809-1817).
  - `usePersistentState` hook already exists (used at ~1193 `usePersistentState<TnEvent[]>("tn.events", [])`).
- Tests: `tests/test_thailandnow_wpop.py` (endpoint tests; import endpoint fns directly,
  `@pytest.mark.anyio`, `monkeypatch.setattr("app.thailandnow.<helper>", async_mock)`).
  `tests/test_thailandnow_events.py` (parse/dedup unit tests).

## Google Sheets facts (verified live by the host — do not re-verify)

- OURS sheet id: `1Hk3o7eui5S_fvC7ptZWZceT3PBIT9SUQPf_iMkddXoI`. Its FIRST tab is titled
  `Thailand NOW — Covered Events Registry (WP-published)` (em-dash!). For writes to it use
  NAMELESS A1 ranges (e.g. `A1:E`) — nameless = first tab, avoids quoting the title.
- A new second tab named exactly `Pipeline` must be created on demand (batchUpdate
  `{"requests":[{"addSheet":{"properties":{"title":"Pipeline"}}}]}`, then write its header row
  `Provisioned At | Event Title | Slug | Trello Card | Doc Link | Status` — only if the tab
  was just created).
- The existing token (scopes documents+drive) IS authorized for sheet writes (canary 200).
- API shapes: append = `POST .../values/{range}:append?valueInputOption=RAW` body
  `{"values":[[...]]}`; update = `PUT .../values/{range}?valueInputOption=RAW` same body;
  base `https://sheets.googleapis.com/v4/spreadsheets/{id}`.
- COMPANY sheet `1LO32cJTCSN0XEUPiuEjQmeWr0LU-ohY7ca1GWQv2-N8` stays READ-ONLY. Never write it.

## Build — backend (`app/thailandnow.py`)

1. Helpers beside `_read_sheet_col`:
   - `async _sheet_append_rows(sheet_id, tab, rows: list[list[str]]) -> None` — append rows
     under existing data of `tab` (range `f"{tab}!A1"`). 502-style raise on non-200.
   - `async _sheet_update_cell(sheet_id, tab, row_number: int, col_letter: str, value: str) -> None`
     — PUT `{tab}!{col}{row_number}`.
   - `async _sheet_read_all(sheet_id, tab) -> list[list[str]]` — GET `{tab}!A1:F10000`, return
     raw values (rows may be ragged; missing cells = "").
   - `async _ensure_pipeline_tab() -> None` — GET spreadsheet `?fields=sheets.properties.title`;
     if no tab titled "Pipeline", batchUpdate addSheet + append the header row above.
   - `async _pipeline_find_row(slug: str) -> int | None` — `_ensure_pipeline_tab()`, read all,
     return the 1-based sheet row number of the first data row whose Slug column
     (col C, index 2) `_covered_slug`-matches `slug`; None if absent.
2. `POST /api/thailandnow/events/registry/sync` (new endpoint, place near the covered GET):
   - Factor `async _wp_pull_published_events() -> list[dict]`: page the PUBLIC WP REST
     `https://www.thailandnow.in.th/wp-json/wp/v2/event?per_page=100&page=N&_fields=slug,date,title,link,id`
     via httpx (no auth) until a short page; title from `title.rendered`.
   - Rewrite the Published (first) tab: values UPDATE `A1:E{max_rows}` where `max_rows` =
     max(new row count incl. header, current populated row count + 1), padding trailing
     cells/rows with "" so no stale tail survives. Rows = header
     `Event Title | Date Published (WP) | Slug | WP Link | WP ID` + events sorted by date asc.
   - Reconcile Pipeline: read Pipeline tab; for each row whose slug is in the published set
     AND Status != "PUBLISHED", `_sheet_update_cell` Status (col F) → "PUBLISHED".
   - Return `{"published_synced": N, "pipeline_flipped": M}`.
3. Hook B — `create_event_doc`: after `provision(...)` returns `res`, if `res.get("items")`:
   inside `try/except Exception` (soft-fail — provisioning must never break), call
   `_ensure_pipeline_tab()` + `_sheet_append_rows(OURS, "Pipeline", [[today_iso, title,
   _covered_slug(title), card_url, doc_url, "PIPELINE"]])` from `res["items"][0]`
   (today_iso = local date). Merge `"registry": "pipeline-logged"` into the returned dict on
   success, `"registry": "skipped: <err>"` on failure.
4. Hook A — `publish_event_from_card`: ONLY when `kind == "event"`, between the WP-success
   block (~5378) and the return: soft-fail block that computes `slug_key = _covered_slug(title)`
   (fall back to the WP `slug` var's `_covered_slug` if title empty), finds the pipeline row;
   if found → `_sheet_update_cell` Status → "DRAFT"; if not found → append a new row
   `[today_iso, title, slug_key, "", permalink, "DRAFT"]`. Merge `"registry": "draft-logged"|"skipped: <err>"`
   into the return dict.
5. `_covered_events()`: third try/except block reading OURS "Pipeline" tab —
   `_sheet_read_all(OURS, "Pipeline")`, for each data row take col C (index 2) slug and
   `out.setdefault(slug, "pipeline")` (AFTER ours+company blocks — published truth wins).
   Missing tab / read failure lands in `errors` like the others.

## Build — frontend (`frontend/src/components/ThailandNowPanel.tsx`, EventsTab only)

1. `const [hideCovered, setHideCovered] = usePersistentState<boolean>("tn.events.hideCovered", true);`
2. Extract the covered fetch into `const loadCovered = useCallback(async () => {...}, [])`
   (same fetchJSON + setCovered, non-fatal catch); mount effect calls it.
3. `sorted` useMemo: after the existing sort, when `hideCovered` filter out rows where
   `covered[slugCovered(e.title)]` exists AND its value is "ours" or "company" (pipeline rows
   stay visible). Header: `RESULTS{sorted.length ? ` · ${sorted.length}` : ""}` gains
   `, `${hiddenCount} covered hidden`` when hiddenCount > 0 (compute hiddenCount from
   events.length vs visible). Add an `✕/☐ HIDE COVERED` toggle button beside CLEAR
   (`btn btn--compact` + `btn--signal` when on) flipping `setHideCovered((v) => !v)`.
4. ✓ COVERED badge (~1809): condition becomes value is "ours" || "company". Add a sibling
   ⚙ PIPELINE badge when value === "pipeline" (`label`, color `var(--color-go)`,
   title "in pipeline (see Pipeline tab)").
5. Mode-toggle row (~1526): add `↻ SYNC REGISTRY` button (`btn btn--md`); onClick sets a
   syncing flag, POSTs `/api/thailandnow/events/registry/sync` (fetchJSON), then
   `await loadCovered()`, flashes the synced count in the muted hint span; non-fatal catch.

## Build — tests (offline only; no real network)

Update `tests/test_thailandnow_wpop.py` (+ `test_thailandnow_events.py` if useful):
- Existing `test_publish_event_from_card_happy_path` and any test that now traverses Hook A/B:
  add `monkeypatch.setattr("app.thailandnow._sheet_update_cell", async_mock)` /
  `_sheet_append_rows` / `_sheet_read_all` / `_ensure_pipeline_tab` no-op async stubs so NO
  real HTTP happens in the suite; assert the happy path returns `registry: "draft-logged"`.
- New: Hook B appends the right row (capture args of `_sheet_append_rows`); Hook A flips an
  existing row via `_pipeline_find_row` mock → `_sheet_update_cell` called with ("Pipeline", row, "F", "DRAFT");
  covered reader mixes pipeline source with setdefault precedence; sync endpoint with mocked
  `_wp_pull_published_events` (+ mocked sheet read/update helpers) returns counts and pads
  the rewrite range.

## Constraints

- Do NOT touch the COMPANY sheet, `provision()`, other panels, or unrelated files.
- Comments only where a constraint isn't obvious from code. Match existing style.
- Backend must import nothing new beyond what `app/thailandnow.py` already imports (httpx,
  urllib, datetime all present).
- No UI redesign — reuse existing classes/colors exactly.
