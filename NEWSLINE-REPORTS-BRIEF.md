# NEWSLINE Reports — build brief

Single-sourced contract for the **NEWSLINE Reports** module in **Railjack** (home hub).
Mirrors the RADIO module (`RADIO-BRIEF.md`, `app/newsroom.py`, the RADIO tab in
`NewsroomPanel.tsx`). **Read `RADIO-BRIEF.md` + the existing RADIO implementation first**
and reuse its shape — do not invent a new pattern.

## What & why
Auto-generate Naz's monthly NBT contractor work-report docs. Today he **duplicates two
template docs by hand each month and fills them** — pure tedium. This generates them from
a period + date range. (Monthly NEWSLINE work-log = the #1-ranked "NEWSROOM doc automation" pain.)

## Inputs (per generation)
- **period no.** (`งวดที่`) — Naz supplies (contractor-period scheme; do NOT auto-derive).
- **date range** — `start` + `end` (Thai-gov contractor periods, e.g. 21 Feb – 20 Mar). Naz supplies.
- **fiscal year (BE)** — **auto-derive** from the start date:
  `FY_BE = CE_year + 543 + (1 if month >= 10 else 0)` (Thai FY starts **Oct 1**).

## Outputs — two docs, duplicated from templates, filled, saved to the report folder
- **Destination folder:** `1aregEEnnZPm2JhP2_-S0X03f8a-5ViuN` (`n.rojanasuvan@gmail.com`).
  (Optional: a fiscal-year subfolder when the FY rolls over Oct 1.)
- **Templates to duplicate (Drive `files.copy`, preserves .docx + QR image):**
  - **(A) cover** — `1vqhrBRUUgbDSX9PoNBaRWDr6mqPSSgU7`
    ("### ใบรายงานผลการปฏิบัติงาน แบบ QR Code … ณอรรฆย์ โรจนสุวรรณ")
  - **(C) log** — `1FFRqsOV8XdgDPAlM0u0Vyzak8LyN71bc`
    ("### รายงานผลการปฏิบัติงาน MM 256X …")

### (A) cover — fill
Static constants already in the template (leave as-is): name `นายณอรรฆย์ โรจนสุวรรณ`,
role `ผู้ผลิตรายการข่าวภาษาอังกฤษ`. Dynamic: **period no.** + **date range** rendered as
`D <Thai-month> <BE-year>` in **western numerals**
(e.g. `21 กุมภาพันธ์ 2569 – 20 มีนาคม 2569`). QR image is in the template — keep it.

### (C) log — fill
- **Header:** period no., fiscal year (BE), name/position/division (constants), date range.
- **Body:** **one row per weekday Mon–Fri** in the range. Each row:
  `<D> <Thai-month> <BE-year>  รายการ NEWSLINE` — dates in **Thai numerals**
  (e.g. `๑ ตุลาคม ๒๕๖๘  รายการ NEWSLINE`). Replace the template's 3 sample days with the
  full month's weekdays.
- **Mon–Fri only — INCLUDE public holidays** (Naz works holidays). No source-doc lookup; every
  weekday row is always `รายการ NEWSLINE`.

### Naming + placement
Substitute the real month/year for the template placeholders (drop the `###` / `MM 256X`
markers), e.g. `ใบรายงานผลการปฏิบัติงาน แบบ QR Code สิงหาคม 2569 ณอรรฆย์ โรจนสุวรรณ`.
Save both into the destination folder.

## Date formatting (implement once, one helper)
- **Thai month names:** มกราคม กุมภาพันธ์ มีนาคม เมษายน พฤษภาคม มิถุนายน กรกฎาคม สิงหาคม กันยายน ตุลาคม พฤศจิกายน ธันวาคม
- **Thai numerals:** `๐๑๒๓๔๕๖๗๘๙` — used in the **log (C) body dates**.
- **Western numerals** — used in the **cover (A) dates**.
- **BE year** = CE year + 543.

## Backend (Railjack — `app/newsroom.py` shape: `create_subprocess_exec`, `_json`/`_fail`)
- `POST /api/newsroom/newsline-reports/preview` `{period, start, end}` → pure compute, no write.
  Returns the planned doc set: filenames + the enumerated Mon–Fri rows (Thai-numeral preview).
- `POST /api/newsroom/newsline-reports/generate` `{period, start, end}` → duplicates both
  templates, fills, saves to the destination folder; returns `{cover:{id,url}, log:{id,url}}`.
- **Idempotent:** if docs for that (period, FY) already exist in the folder, return them —
  never double-create.
- **Auth:** railjack RW Drive token (`~/.config/railjack/google_token.json`, full-drive scope) —
  **reuse RADIO's Drive helper.** Never print or commit the token.
- Drive/Docs: `files.copy` to duplicate → Docs `batchUpdate` (`insertText` / find-replace) to
  fill. The log body's weekday rows are generated text inserted in place of the sample rows.

## Frontend (Railjack)
New **"NEWSLINE REPORTS"** tab in `NewsroomPanel.tsx` (next to RADIO). UI: period-no. input +
date-range pickers + **Preview** (shows the filenames + the Mon–Fri Thai-numeral list) +
**Generate** (creates both docs, shows their links). Model on the RADIO panel; raw
`fetch('/api/newsroom/newsline-reports/…')` with local error state.

## Reference implementation
**RADIO** (`RADIO-BRIEF.md`, `app/newsroom.py`, its `NewsroomPanel.tsx` tab). Read it and reuse
the Drive helper, the panel-tab registration, and the preview → confirm → generate gate.
Implement-not-reinvent.

## Verify (run it — never code-read only)
1. `preview` for a period in Aug 2026 → correct weekday list (Thai numerals), correct
   filenames, **FY 2569**.
2. `generate` → 2 new docs in the report folder; cover dates **western**-numeral, log body
   **Thai**-numeral Mon–Fri (incl. any holidays), header constants correct, QR intact on cover.
3. Re-run `generate` for the same period → **idempotent** (no duplicates, returns existing).
4. **FY boundary:** a period crossing Oct 1 → correct FY bump (2569 → 2570).
5. No secret leakage — the railjack token is never logged or committed.

## Out of scope (v1)
- The **rundown compilation** doc (pains 1+2) — separate later build.
- **Holiday auto-skip** — Naz works holidays; list all Mon–Fri.
- **NBT WORLD BRIEF** — report is Newsline-only per Naz.
- **Auto-deriving the period number** — Naz supplies it.
