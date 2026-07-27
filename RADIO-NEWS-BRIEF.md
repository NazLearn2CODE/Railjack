# RADIO ▸ NEWS FILL — build brief

Second operation under the RADIO tab (the first is the shipped **Document Generator**).
**News Fill** scouts global or business news from a fixed outlet pool, lets Naz tick the pieces he
wants, and drops each into the correct slot of a chosen day's **script doc** (the docs the Document
Generator produces).

The script docs are pre-built skeletons with **native Google-Docs tabs** `AM / MIDDAY / EVE`. Each
tab has `HEADING_1` sections (`NATIONAL NEWS`, `GLOBAL NEWS`, `BUSINESS NEWS`); under each section
are `HEADING_2` placeholders literally reading `1.[]`, `2.[]`, … , each followed by an **empty**
`NORMAL_TEXT` line and a `--` line. Each placeholder is preceded by a **label** line
(`Global/AM/CALENDAR`, `Business/AM/CALENDAR`, `Local Biz/AM/CALENDAR`, `Slice of Life/AM/CALENDAR`,
`National/…`). **This module fills GLOBAL and BUSINESS slots only** (never NATIONAL / Local Biz).

This brief is the single source of truth for BOTH builders. The **Contract** section is binding.
Task A (backend, GLM-5) and Task B (frontend, Antigravity IDE) build their halves against it. The
`radio-news-scout` Claude skill (the scout that writes the handoff file) is authored separately by
the host — builders only depend on the **handoff JSON shape** below.

---

## Contract (binding for both halves)

### Fill-map — which slots this module fills, per (category, kind)
| Tab | GLOBAL slots | BUSINESS slots |
|-----|--------------|----------------|
| **AM** (weekday only) | 1, 2, 3, **4\*** | 2, 3, 4, 5 |
| **MIDDAY** | 1, 2, 3, 4 | 2, 3, 4, 5 |
| **EVE** | 1, 3, 5 | 3, 5 |

- `*` **AM GLOBAL slot 4 = the Slice of Life slot** (AM-only). It is filled from a **separate**
  Slice-of-Life pick, not the ranked hard-news list.
- **Weekday** targets: GLOBAL = 10 hard (AM 1-3, MIDDAY 1-4, EVE 1,3,5) **+ 1 Slice** (AM 4).
  BUSINESS = 10 (AM 2-5, MIDDAY 2-5, EVE 3,5).
- **Weekend** = skip AM entirely (no Slice of Life). GLOBAL = 7 (MIDDAY 1-4, EVE 1,3,5).
  BUSINESS = 6 (MIDDAY 2-5, EVE 3,5).
- **BUSINESS slot 1 ("Local Biz") is always reserved** — never filled by this module.
- Ranked pieces fill hard slots in tab order **AM → MIDDAY → EVE**, top-ranked into the earliest slot.

`build_slotmap(category, kind) -> list[(tab, slotNumber)]` must be a **pure** function (unit-tested)
returning exactly the ordered hard slots above. The Slice slot (`("AM", 4)`) is handled separately,
not in this list.

### Slot locator (how the fill script finds a placeholder — CRITICAL)
Placeholders `N.[]` **repeat within a tab** (National has 1-4, Global has 1-4, Business has 1-5), so
a bare text match on `N.[]` is ambiguous. Anchor on the **label line + number**:

- Walk `documentTab.body.content` paragraphs in order. Track the most recent **label** paragraph:
  a `NORMAL_TEXT` whose text matches `^(National|Global|Business|Local Biz|Slice of Life)/`.
- When you hit a `HEADING_2` whose text is exactly `N.[]` (after strip), its **category** is the
  label's prefix token and its **number** is `N`. Match the label **prefix only** (the label's
  suffix is `CALENDAR` in the template but a real date in filled docs — match `startswith`).
- Map GLOBAL fill to the `Global/…` label; BUSINESS fill to the `Business/…` label; the Slice pick to
  the `Slice of Life/…` label. `Local Biz` and `National` labels are never targeted.

### What to write into a located slot
- **Title** goes **inside the brackets**: turn `N.[]` into `N.[<title>]` (insert title between `[`
  and `]`).
- **Content** = the article body, inserted into the **empty `NORMAL_TEXT` line directly below the
  `N.[]` heading and above the `--` line**. Multi-paragraph bodies: insert with `\n` separators
  (Docs turns each `\n` into a paragraph). **Leave the `--` line as-is** (it is the writer's byline
  placeholder — never auto-add `— name`).
- **Format = ctrl+shift+v equivalent**: plain `insertText` only. **Do NOT** send `updateTextStyle` —
  inserted text inherits the destination paragraph style (heading style in the bracket, normal in the
  body). This is the whole point of "paste without formatting."
- **Slice of Life (weekday global only):** when a `slice` piece is supplied, fill AM GLOBAL slot 4
  as above **and** fill its lead-in line — the paragraph
  `Today on Radio Thailand's Slice of Life: Bringing You a Brighter Day, we have [ARTICLE HEADLINE] from [SOURCE].`
  Replace `[ARTICLE HEADLINE]` with the slice title and `[SOURCE]` with the slice source.
- **Idempotency:** if a target slot's brackets are already **non-empty** (`N.[` followed by
  non-`]`), **skip** it and report it in `skipped` — never double-insert.

### Index-shift rule (batchUpdate)
All inserts for one tab go in **one `batchUpdate`**, with requests **sorted by descending
`index`** so an earlier insert never shifts a later insert's location. (Docs applies requests in
array order; descending index keeps every precomputed index valid.) One batchUpdate per tab is fine;
do not interleave tabs in a single request list unless every location carries its own `tabId`.

### Handoff JSON (written by the `radio-news-scout` skill; read by the backend)
Path: **`/tmp/railjack-radio-news/latest.json`**. One JSON **object**:
```json
{
  "category": "global",
  "results": [
    {"title": "…", "url": "https://…", "source": "CNN",
     "date": "2026-07-27", "content": "para 1\n\npara 2\n\npara 3", "words": 240}
  ],
  "slice_of_life": [ { "…same shape…" } ]
}
```
- `results`: ≥20 items, each `words ≥ 190`, dated (`YYYY-MM-DD`), **sorted most-recent first**.
- `slice_of_life`: exactly the lighter suggestions (5), **global runs only**; omit/empty for business.
- `content` is the cleaned article body (already fetched) — the backend never re-fetches.

### Auth
Reuse the **railjack read-write token** `~/.config/railjack/google_token.json` (documents + drive
scope — same token as the Document Generator's `radio.py`). Refresh exactly like
`radio.py:google_token()` / `app/thailandnow.py:_google_token`. Do **not** use the newsroom token
(`~/.config/newsroom/google_token.json`; it is `drive.readonly`).

### Parent Drive folder (for the doc picker)
`1LSw5NwDhwg7PE9pJUO6jKPcd3yFBCOI9` ("RT 2026"). Script docs live one level down inside month folders
named `YYYYMM MonthName`. `kind` + `date` parse from the doc name `YYYYMMDD_Weekday Script` /
`YYYYMMDD_Weekend Script`.

---

## Task A — BACKEND (delegated to GLM-5 via agent-x)

House style: stdlib-only script (`urllib`, `json`, `argparse`, `calendar`, `datetime`, `re`),
JSON-to-stdout, `{"_fatal": …}`+exit-1 for handled errors. Read before writing:
`~/Cephalon/10-knowledge/skills/newsroom/scripts/radio.py` (token + Drive REST + `_fatal`),
`~/Cephalon/10-knowledge/skills/newsroom/scripts/nl_append.py` (tab-aware Docs read + `insertText` +
`batchUpdate`), `app/newsroom.py` (`_run`/`_json`/`_fail`/`_script` thin-wrapper idiom),
`app/terminal_input.py` (already provides `/api/terminal/insert` — do NOT re-add it).

### 1. Skill script `newsroom/scripts/radio_news.py` (write VAULT path first, then byte-identical copy)
Vault: `/home/NAZ/Cephalon/10-knowledge/skills/newsroom/scripts/radio_news.py`; then copy
byte-identical to `/home/NAZ/.claude/skills/newsroom/scripts/radio_news.py`. Subcommands:

- **`list-docs [--parent ID] [--limit N]`** — Drive: list month folders under the parent, then the
  `application/vnd.google-apps.document` files inside each (`supportsAllDrives=true`,
  `includeItemsFromAllDrives=true`, paginate). Parse `kind` (`weekday|weekend`) + `date`
  (`YYYY-MM-DD`) from each name; drop non-`_Weekday/_Weekend Script` names. Sort by date desc, cap
  `--limit` (default 60). Print `{"docs":[{"id","name","link","kind","date"}]}`.
- **`report [--path P]`** — read the handoff file (default `/tmp/railjack-radio-news/latest.json`).
  `_fatal` "no scout handoff yet — run the scout in the terminal" if missing; `_fatal` on bad JSON.
  Normalize each item to `{title,url,source,date,content,words}`, dedup on `url`. Print
  `{"category","results","count","slice_of_life","mtime"}` (`mtime` = file mtime float).
- **`fill --doc ID --kind weekday|weekend --category global|business`** — read pieces JSON from
  **stdin**: `{"pieces":[…ranked hard picks…], "slice": {…}|null}`. Implement `build_slotmap`, the
  slot locator, the title/content/slice/idempotency rules, and the descending-index batchUpdate per
  the Contract. Print `{"doc_id","category","written":[{"tab","slot","title"}],"skipped":[{"tab","slot","reason"}]}`.
  `process(piece) -> (title, body)` is **pass-through now** (title = `piece["title"]`,
  body = `piece["content"]`); leave a clearly-marked seam for the future glm-5 gem call.

Keep `build_slotmap` and the pure locator logic import-safe (no network at import) so tests can call
them directly, mirroring how `test_newsroom.py` loads `radio.py` by path (`_load_radio`).

### 2. `app/radio_news.py` — new thin router module (mirror `app/newsroom.py`)
- `SCRIPTS = Path.home()/"Cephalon"/"10-knowledge"/"skills"/"newsroom"/"scripts"`;
  `RNEWS = SCRIPTS/"radio_news.py"`; `PY = "python3"`. Reuse the `_run`/`_json`/`_fail`/`_script`
  idiom (import from `app.newsroom` or restate — your call, keep it thin).
- `GET  /api/newsroom/radio/news/docs`  → `_script([PY, str(RNEWS), "list-docs"])`.
- `GET  /api/newsroom/radio/news/report`→ `_script([PY, str(RNEWS), "report"])`.
- `POST /api/newsroom/radio/news/apply` body `{doc_id, kind, category, pieces:[…], slice?:{…}}` →
  400 if `doc_id`/`kind`/`category` missing; argv
  `[PY, str(RNEWS), "fill", "--doc", doc_id, "--kind", kind, "--category", category]`, feeding
  `json.dumps({"pieces":…, "slice":…})` to the child's **stdin** (`create_subprocess_exec(...,
  stdin=PIPE)` + `communicate(input=payload)`), `timeout=180`.
- Register in `app/main.py`: `from .radio_news import router as radio_news_router` +
  `app.include_router(radio_news_router)`.

### 3. `tests/test_newsroom.py` — add News-Fill tests (match existing style; load the script by path)
- `build_slotmap` for all four (category, kind) cases returns the exact ordered slot lists above.
- Slot locator against a small captured template-structure fixture (paragraph list of
  `(namedStyleType, text)`): resolves `Global/AM → 3.[]` to the right (tabId, bracket idx, body idx)
  and does **not** collide with the identically-numbered `National`/`Business` placeholders.
- Slice-of-Life lead-in fill produces both the `[ARTICLE HEADLINE]` and `[SOURCE]` replacements.
- `report` normalization: `slice_of_life` passes through; dedup on url; `_fatal` when file absent.
- `list-docs` name parsing → kind/date, non-script names dropped.
- `apply` route: 400s on missing `doc_id`/`category`/`kind`; happy path passes the right argv +
  stdin JSON (monkeypatch `_run` to capture argv **and** input).

**Landing bar:** `pytest tests/test_newsroom.py` green. Do NOT do a live Docs write during the build
(no real `fill` against a real doc) — the host verifies that. `list-docs`/`report` may be exercised
with monkeypatched Drive/file access only.

---

## Task B — FRONTEND (delegated to Antigravity IDE)

File: `frontend/src/components/NewsroomPanel.tsx`. The existing **RADIO** tab becomes a **two-mode**
panel. Keep phosphor/HUD styling and reuse the existing `fetchJSON`/`post`/`error` helpers.

- Inside the `{tab === "radio" && (...)}` block, add a **mode toggle** at the top:
  **Document Generator** (the existing year/month/PREVIEW/GENERATE UI, unchanged) | **News Fill** (new).
- **News Fill** mode:
  0. **Working document** dropdown — `GET /api/newsroom/radio/news/docs` → list `{name}`; store the
     chosen `{id, kind}`. (Show newest first.)
  1. **Category** dropdown `global | business` + **SCOUT** button → `POST /api/terminal/insert`
     `{text: "/radio-news-scout " + category}` (this types the command into the tmux pane; Naz presses
     Enter himself). Then a **CONVERT** button → `GET /api/newsroom/radio/news/report`.
  2. Render `results` as a **numbered checklist** (reuse a `selected` record keyed by `url`, like the
     Story-Scout image basket). Show the **target hard count** for the chosen (category, kind):
     weekday global 10 · weekday business 10 · weekend global 7 · weekend business 6.
     On a **weekday + global** doc, also render a **Slice of Life** section listing `slice_of_life`
     with single-pick radios (pick exactly 1).
  3. **CONFIRM** — enabled only when the hard target is met (and, if applicable, one Slice picked) →
     `POST /api/newsroom/radio/news/apply` `{doc_id, kind, category, pieces:[…selected in rank order…],
     slice:{…}|null}`. Render `written` (tab · slot · title) and any `skipped`.
- Surface backend `_fatal`→400 detail via the shared `error` state, like the other calls.

**Landing bar:** `npm run build` (tsc + vite) passes; on `:8700` NEWSROOM → RADIO → News Fill: pick a
doc, SCOUT types the command, CONVERT lists results, CONFIRM stays disabled until the target is met.

---

## Both: do not
- Do not touch the Document Generator, queue/append/rewrite logic, or the newsroom readonly token.
- Do not re-add `/api/terminal/insert` (it already exists in `app/terminal_input.py`).
- Do not do a live Docs write during the build — the host runs the first real fill.
- Do not add `updateTextStyle` in the fill — plain `insertText` only (format inherits = ctrl+shift+v).
- Restart the hub after route changes: `systemctl --user restart railjack.service` (no hot-reload;
  405 on the new POST = you forgot).
