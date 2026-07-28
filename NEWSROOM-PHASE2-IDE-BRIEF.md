# NEWSROOM ▸ Phase 2 — SEND TO [NL|RADIO] slot-fill + unified bold/underline

Phase 1 shipped Ben-voice REWRITE + `**name**`/`-/date/-` markers + AI SEO (Railjack
`a7a0bf8`/`362987a`, vault `8c3a963`). Phase 2 makes those markers **do something** in two
target docs, unifying on ONE model: **fill story slot #N**, with bold/underline styling applied.
Full context in `RADIO-BRIEF.md` + `NEWSROOM-REWRITE-BEN-IDE-BRIEF.md`. Spawned from `~/Cephalon`;
follow `AGENTS.md`. Build order below; can be one pass or sliced.

## Locked decisions (Naz, 2026-07-28)
1. **SEND TO NL** → pick tab (AM/MID/EVE/NL) + story slot #N → **fill/replace that slot's body**.
2. **SEND TO RADIO** → pick section (AM/MIDDAY/EVE) × block (National/Global/Business) × slot #N →
   fill the `## N.[]` slot; swap `CALENDAR`→today.
3. **Markers apply to both** via ONE shared helper (the single-pass `parse_markers` + bold/underline
   span requests) factored out of `nl_append.append()`. Zero extra LLM calls.

## Key findings (verified LIVE 2026-07-28 — trust these over any stale comment)
- **NL rundown doc** = exactly named `NL & NWB DDMMYY` (today `NL & NWB 280726`,
  id `1--WCM9P4NNODO63RJx1YyBTI_nYokjpUTDM7lF-SwEI`). **4 tabs**, resolved by TITLE:
  `NBTWB AM RUNDOWN` / `NBTWB MID RUNDOWN` / `NBTWB EVE RUNDOWN` / `NL RUNDOWN`. Tab IDs vary per
  doc — **never hardcode**; resolve by title.
- **NL tabs are slot-based**: story headings `1.`–`12.` (HEADING_1), e.g.
  `1. Royal Birthday Ceremony Held at Sanam Luang`. Slot #N = heading whose text matches
  `^N\.\s`. Slot body = content from that heading's `endIndex` to the **next** heading's
  `startIndex` (or tab end). `***END CREDIT***` is a table cell inside the NL tab — irrelevant to
  slot-fill (that was the old bottom-append behaviour).
- **`find_today_doc` BUG** (`nl_append.py`): `name contains 'NL & NWB DDMMYY'` + `orderBy
  modifiedTime desc` matches TWO files and picks the wrong one (`RUNDOWN NL-NWB DDMMYY`, a separate
  single-tab brief). Drive `contains` is tokenized/fuzzy. **Fix = exact `name = 'NL & NWB DDMMYY'`.**
- **Radio script doc** (weekday template `1QuFzKF8…`, weekend `1insfe6Z…`): single body, sections
  `# AM` / `# MIDDAY` / `# EVE` (H1) → blocks `***NATIONAL NEWS***` / `***GLOBAL NEWS***` /
  `***BUSINESS NEWS***` (H1) → slots `## 1.[]`, `## 2.[]`… (H2), each tagged
  `Category/Section/CALENDAR`, separated by a `--` line. Slot #N under [section×block] = the Nth
  `## N.[]` heading. **Caveat:** verify the daily copy is single-body (no tabs) on the first live
  fill — `find_heading` must handle `tab_id=None` (body) OR a resolved tab.
- **Auth**: NL fill reuses `nl_append.token()` (has Docs write access — today's SEND TO NL proves
  it). Radio fill reuses `radio.py::google_token()` (railjack RW, full drive). Shared helpers are
  **token-agnostic**.

## Contract

### NEW shared module `docfill.py` (vault scripts dir; `nl_append` + `radio` both import it)
Token-agnostic: callers pass an `api_get(url)->dict` and `api_post(url,body)->dict` (or one `api`
callable + token). Pure wherever possible.
- `parse_markers(text) -> (plain, bold_ranges, underline_ranges)` — **move** from `nl_append`
  (the single-pass, bug-fixed version). `nl_append` re-imports it so the existing append path is
  unchanged.
- `find_heading(api_get, doc_id, tab_id, match) -> dict | None` — GET
  `documents/{id}?includeTabsContent=true&fields=tabs(tabProperties(tabId,title),documentTab(body(content(startIndex,endIndex,paragraph(elements(textRun(content)),paragraphStyle(headingId,namedStyleType))))))`.
  Walk the tab's body; return the first paragraph where `match(text, style)` is true, as
  `{startIndex, endIndex, text, next_start}` (`next_start` = next heading's startIndex, else tab
  endIndex — the slot body range). `tab_id=None` → first/single tab body.
- `build_fill_requests(tab_id, at, plain, bolds, underlines) -> list` — the request list currently
  inline in `nl_append.append()`: `[insertText at, clear-bold across insert, bold spans, underline
  spans]`. `tab_id` optional (omit `tabId` from each range when None).
- `replace_and_style(api_post, doc_id, tab_id, del_start, del_end, at, plain, bolds, underlines)` —
  ONE `batchUpdate`: `deleteContentRange{del_start,del_end}` (only if `del_end>del_start`), then
  `insertText` at `at`, then clear-bold, bold spans, underline spans. (Delete-before-insert in one
  batch keeps indices valid: `at` ≤ `del_start`.)

### `nl_append.py` (vault → byte-identical cc copy)
- **Fix `find_today_doc`**: `name = 'NL & NWB %s' % date` (exact). Keep `orderBy` or drop it.
- **Generalize tab resolution**: make `nl_tab`'s title a param → `find_tab(tok, doc_id, title)`;
  default `NL_TITLE = "NL RUNDOWN"`. Existing append path still resolves NL RUNDOWN.
- **New `fill_nl_slot(tok, doc_id, tab_title, slot_n, text)`**: `find_tab` → `find_heading`
  matching `^{slot_n}\.\s` → body = `[h.endIndex, h.next_start)` → `parse_markers(text)` →
  `docfill.replace_and_style(... del_start=h.endIndex, del_end=h.next_start, at=h.endIndex ...)`.
  `_fatal` if the slot heading isn't found.
- **CLI**: `nl_append.py fill --today [--date DDMMYY] --tab NL --slot 3 --text "..." [--doc ID]
  [--dry-run]` (dry-run prints the resolved tab/heading/range + span counts, no write).
- Keep `append()` (bottom-of-NL) for backward compat.

### `radio.py` (vault → byte-identical cc copy)
- **New `find_day_doc(parent, yyyymmdd)`**: `find_month_folder(year, month)` → `existing_names` →
  pick `YYYYMMDD_Weekend Script` if `date.weekday()>=5` else `YYYYMMDD_Weekday Script`; return id.
  `_fatal` if absent ("run RADIO GENERATE for this month first").
- **New `fill_radio_slot(year, month, day, section, block, slot_n, text)`**: `find_day_doc` →
  `find_heading` section (`AM`/`MIDDAY`/`EVE`) → `find_heading` block (`***NATIONAL NEWS***` etc,
  scoped after the section) → Nth `## N.[]` under it → insert text at that heading's `endIndex`
  (before the following `--`/next heading) via `docfill.replace_and_style` (**insert, no delete**:
  `del_start==del_end==at`) → replace the slot's tag-line `CALENDAR` with today's date.
  Resolve headings by text (`# AM`, `## N.[]`); `_fatal` with the resolved doc name on any miss.
- **CLI**: `radio.py fill --year --month --day --section AM --block NATIONAL --slot 2 --text "..."
  [--parent ID] [--dry-run]`. Keep the existing batch `generate`/`preview` untouched.

### `app/newsroom.py` (reuse `_script`; restart railjack after edits — no --reload)
- `POST /api/newsroom/fill` body `{text, tab?, slot, doc_id?}` →
  `[PY, APPEND, "fill", "--tab", tab or "NL", "--slot", str(slot), *(--doc/--today), "--text", text]`.
  400 if `slot` missing/non-int. timeout 60.
- `POST /api/newsroom/radio/fill` body `{text, year, month, day, section, block, slot}` →
  `[PY, RADIO, "fill", "--year", ..., "--section", ..., "--text", text]`. 400 on missing
  year/month/day/section/block/slot. timeout 60. No change to `radio/preview`/`generate`.

### Frontend `frontend/src/components/NewsroomPanel.tsx`
- **SEND TO NL**: add a tab `<select>` (AM/MID/EVE/NL, default NL) + slot `<input type=number>`;
  posts `{text, tab, slot}` to `/api/newsroom/fill`. The current bottom-append can stay as a small
  secondary control if trivial; otherwise SEND TO NL = slot-fill.
- **SEND TO RADIO** (new button beside SEND TO NL): section `<select>` (AM/MIDDAY/EVE) + block
  `<select>` (National/Global/Business) + slot `<input type=number>`; year/month/day default today;
  posts `{text, section, block, slot, year, month, day}` to `/api/newsroom/radio/fill`.
- Reuse existing `post`/`fetchJSON`/`error` state; surface `_fatal` (→400) like the other calls;
  keep the terminal/phosphor styling.

## Build order (recommended)
1. `docfill.py` + `find_today_doc` exact-match fix (unblocks everything; smallest blast radius).
2. NL slot-fill (`fill_nl_slot` + CLI + `/api/newsroom/fill`).
3. Radio slot-fill (`find_day_doc` + `fill_radio_slot` + CLI + `/api/newsroom/radio/fill`).
4. Frontend (both buttons). Restart railjack before live UI test.

## Landing bar
- `uv run pytest tests/test_newsroom.py -q` green. Add tests:
  - `find_today_doc` exact match picks the 4-tab `NL & NWB` doc, NOT `RUNDOWN NL-NWB` (stub the
    Drive list call to return both).
  - `fill_nl_slot` builds `[deleteContentRange, insertText, clear-bold, bold…, underline…]` with
    **aligned** spans incl. a `**name**` AFTER a `-/date/-` (the Phase-1 regression must hold here).
  - `find_day_doc` picks Weekday vs Weekend by `date.weekday()` (Sat/Sun → Weekend).
- `npm run build` (tsc + vite) clean.
- Live on :8700: SEND TO NL → tab+slot replaces the right story body, names bold + dates
  underlined; SEND TO RADIO → section+block+slot fills `## N.[]`, CALENDAR→today. `find_today_doc`
  no longer grabs the wrong doc.
- Vault scripts written to vault path FIRST, then byte-identical to `~/.claude/skills/newsroom/scripts/`.

## Do not
- Don't change REWRITE/SEO/queue logic. Don't touch RADIO monthly `generate`/`preview`.
- Don't hardcode tab IDs (resolve by title). Don't assume the Radio daily doc is single-body —
  `find_heading` handles `tab_id=None` and the tear-test confirms on first live fill.
- Markers only (`**name**`, `-/date/-`); no transliteration; no extra LLM call.
- Commit only — don't push Railjack (host's call). Don't edit Ben's gem or the SEO gem.
