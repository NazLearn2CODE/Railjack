# BRIEF — Thailand NOW Daily Traffic OP (TRAFFIC sub-tab, hub side)

Railjack (FastAPI hub + React cockpit). Target repo: `/var/home/NAZ/Coding Projects/Railjack`.
You are a single-turn builder: implement EXACTLY this brief, no extras, no formatting churn
outside touched regions. Follow existing code idioms in each file. When done, run nothing —
the host gates you. Companion checkpoint: `TN-DAILY-TRAFFIC-CHECKPOINT.md` (host-maintained).

## Feature (settled design — do not reinterpret)

Replaces a manual daily ritual (open GA4 → read cumulative Total Users since contract start
→ type it into a Google Sheet row → daily diff). New **TRAFFIC sub-tab** in the Thailand NOW
panel + backend endpoints:

1. User picks a date range (default: today only) → **RUN** → backend queries GA4
   `totalUsers` for range 2025-12-05 → *each picked date* (per-date cumulative, NOT summed
   dailies), reads the current sheet state, and returns a **diff preview** (OLD→NEW per
   cell) + a paste-ready one-liner per day. NO writes in this step.
2. User confirms → **apply** writes the sheet (column D always; E only where the cell has
   no formula; auto-append rows past the sheet's last row). COPY TEXT always available
   from preview data.
3. Missing GA config → friendly hint, nothing crashes.

## Config — `~/.config/railjack/ga.json` (host will place it later; code must handle absence)

```json
{
  "client_email": "<sa>@...iam.gserviceaccount.com",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...",
  "property_id": "501926474",
  "sheet_id": "<Analytics & Boosting sheet id>",
  "tab": "2025-26 Web Traffic Report",
  "contract_start": "2025-12-05"
}
```
Loader `_ga_config() -> dict` beside the other config helpers (mirror `_wp_creds` style:
check file, fall back to `_secret("GA_CLIENT_EMAIL")`/`GA_PRIVATE_KEY`/`GA_PROPERTY_ID`/
`GA_SHEET_ID`, HTTPException(503) with a human hint when incomplete). Never log the key.

## Build — backend (`app/thailandnow.py`, new TRAFFIC block placed after the SEO block)

**New dep:** add `pyjwt[crypto]>=2.9` to `pyproject.toml` dependencies (service-account
JWT signing; stdlib has no RSA). Import as `import jwt`.

1. `async _sa_google_token(scope: str) -> str` — service-account → access token: build JWT
   claim `{iss: client_email, scope, aud: "https://oauth2.googleapis.com/token", iat, exp:
   iat+3600}` signed RS256 with the SA key (`jwt.encode`, algorithm="RS256"), POST to the
   token endpoint, return `access_token`. HTTPException(502) on non-200. (Separate from the
   existing `_google_token` OAuth helper — do NOT touch that.) Cache last token+exp in a
   module global; re-mint only when expired.
2. Pure helpers (module level, testable):
   - `_traffic_day(date_iso: str, contract_start_iso: str) -> int` — days since contract
     start, 1-based (Dec 5 → 1).
   - `_traffic_dates(from_iso, to_iso) -> list[str]` — inclusive ISO dates; reject > 92
     days span with ValueError; tz note: callers pass Asia/Bangkok dates.
   - `_traffic_text_lines(rows: list[dict]) -> str` — one-liner per day, exactly:
     `{Mon D} · Day {n} · Total {t:,} · Daily {+d:,} · Target {target:,} (Δ {±gap:,})`
     e.g. `Aug 21 · Day 260 · Total 232,299 · Daily +1,021 · Target 211,258 (Δ +21,041)`.
     Daily 0 → `+0`; first contract day Daily = Total. Missing target → omit the Target
     and (Δ …) parts.
   - `_traffic_proposed_writes(sheet_rows: list[list], dates: list[str], ga: dict[str,int],
     contract_start_iso: str) -> dict` — THE CORE. `sheet_rows` = raw values from the tab
     (header at row 1; A Date "Mmm D", B Day, C Target, D Actual, E Daily; rows may be
     ragged). Returns `{"writes": [...], "appends": [...], "warnings": [...]}` where each
     write = `{row, date, day, target_old, actual_old, actual_new, daily_old, daily_new,
     daily_is_formula}` (daily_new only when the existing E cell is not a formula string
     starting "="; if formula, daily_new=None and the write leaves E alone). Appends (dates
     past the last matching row) = `{date, day, target (copied from last row's value if the
     last row's C is a plain number, else None + warning), actual_new, daily_new}` — daily
     for an append uses the previous row's actual (from sheet or from this run's writes) —
     GA value as yesterday when contiguous. Date matching: sheet A stores "Mmm D" no year —
     match on month+day; if two rows share month+day (year boundary Dec→Jan), match the
     row whose neighbors' days are contiguous (day = row_B − 1). Keep it simple: match by
     expected Day number computed from the date (B column) as primary key, month+day as
     sanity check; if B mismatch, warning + skip.
3. GA query `async _traffic_ga_totals(token, property_id, dates: list[str],
   contract_start_iso) -> dict[str,int]` — **one runReport per date** (deterministic,
   no row-order ambiguity): POST
   `https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport`
   body `{"dateRanges": [{"startDate": contract_start, "endDate": date}],
   "metrics": [{"name": "totalUsers"}]}`; value = `rows[0].metricValues[0].value`
   (`"0"` when rows absent). Concurrency via `asyncio.Semaphore(4)` + gather; ≤92 dates
   per run keeps this bounded. HTTPException(502, f"GA {date}: {status} …") on ≥400.
4. Sheet read/write (Sheets REST, Bearer = SA token, scope
   `https://www.googleapis.com/auth/spreadsheets`):
   - read: GET `https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/
     '{tab}'!A1:E5000?valueRenderOption=FORMULA`
   - apply: batch values.update per write (`PUT .../values/{tab}!A{row}:E{row}`,
     `valueInputOption=USER_ENTERED`, full row A–E so ragged rows get healed) — gather
     with `asyncio.gather(return_exceptions=True)`; appends via ONE
     `POST .../values/{tab}!A1:append?valueInputOption=USER_ENTERED` with all append
     rows in order. 502 on failures; report per-write ok/fail in response.
5. Endpoints (FastAPI, same router, idempotent-friendly):
   - `POST /api/thailandnow/traffic/analyze` body `{from?: str, to?: str}` (defaults:
     today Asia/Bangkok — compute via `zoneinfo.ZoneInfo("Asia/Bangkok")`). Flow: config →
     dates → SA token → GA totals → sheet read → `_traffic_proposed_writes` → return
     `{rows: writes, appends, warnings, text, generated_at, from, to}`. NO writes.
   - `POST /api/thailandnow/traffic/apply` body `{sheet_writes: list[dict],
     appends: list[dict]}` (the client echoes back exactly what analyze returned;
     backend re-validates shapes, ignores unknown keys, caps 92+50 items) → performs the
     writes → `{ok, written, appended, failed: [{row, error}]}`.
   - Pydantic models `TrafficAnalyzeReq`, `TrafficWriteItem`, `TrafficAppendItem`,
     `TrafficApplyReq` beside the other models.
6. Self-check asserts in the `__main__` block beside the SEO ones: `_traffic_day`
   (Dec 5 → 1, Aug 21 2026 → 260), `_traffic_dates` cap + inclusivity,
   `_traffic_text_lines` exact string, `_traffic_proposed_writes`: (a) matching row
   overwrite with non-formula E gets daily_new = diff, (b) formula E stays None,
   (c) stale +0 row gets corrected daily, (d) past-last-row date lands in appends with
   target copied, (e) Day-mismatch row → warning + skip.

## Build — frontend (`frontend/src/components/ThailandNowPanel.tsx`)

1. New top-level tab **TRAFFIC** beside SEO in the panel's tab rail (find where `SeoTab`
   is switched; add `TrafficTab` with label idiom like the others). `TrafficTab` renders
   a `TrafficSubTab` component:
2. Controls row: `from` / `to` `<input type="date">` (default today, tz-local) + **RUN**
   button (busy state, error line in `--color-critical`). Range > 92 days → client-side
   guard with hint.
3. Result: summary line (`{n} days · generated {at}`), then a mono table-ish list per
   write/append row: `Aug 21 Day 260 — Actual: 228,460 → 232,299 · Daily: 0 → +1,021`
   (OLD→NEW; append rows show `NEW ROW`); warnings in `--color-hazard`; formula-E rows
   note `E=formula, untouched`.
4. Buttons: **COPY TEXT** (always; copies `text` from analyze; `navigator.clipboard`
   idiom used elsewhere in this file) and **WRITE SHEET** (`btn--signal`, then an inline
   confirm step like BulkConfirm's pattern → apply → per-write ok/fail summary). On
   success show `✓ {written} written · {appended} appended`.
5. 503 from analyze (config missing) → render the hint text verbatim from the error in
   `--color-muted` (it explains `~/.config/railjack/ga.json`). No crash, no empty spin.
6. New types `TrafficAnalyze`, `TrafficWrite`, `TrafficAppend` mirroring backend shapes;
   reuse `post<T>` helper and existing className idioms only. No new deps.

## Hard constraints

- One new dep (`pyjwt[crypto]`) — nothing else. Add to pyproject + run nothing; host
  installs and gates.
- Private key never logged, never returned in any response or error text.
- analyze performs ZERO writes. apply writes ONLY what the client echoed, within caps.
- Do not touch the SEO block, events code, or any other file except the two named ones
  (+ pyproject.toml).
- Follow `10-knowledge/practices.md`-style testing: pure logic asserted in `__main__`;
  endpoint tests are the host's call — still add none yourself.

## Report (return exactly this shape)

```
FILES CHANGED:
- pyproject.toml — <one line>
- app/thailandnow.py — <one line per addition>
- frontend/src/components/ThailandNowPanel.tsx — <one line per addition>
NOTES: <ambiguities decided, or "none">
```
