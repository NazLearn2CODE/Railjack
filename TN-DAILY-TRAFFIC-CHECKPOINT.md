# Daily Traffic OP — BUILD CHECKPOINT (compact/resume anchor)

Updated: 2026-08-21T12:40+07 · Owner: Tawhan (home) · Status: **IN PROGRESS**

Design settled via grill-me (see Final Decision Summary in session + brief).
Goal armed in DSH (goal-865f5e4f). Briefs: `TN-DAILY-TRAFFIC-ADW-BRIEF.md` (hub) +
`TN-DAILY-TRAFFIC-APP-ADW-BRIEF.md` (app). Two agy builders running in PARALLEL:
hub op (Railjack repo) + app (new repo `~/Coding Projects/tn-daily-traffic`).

## Phase ledger

| # | Phase | Status | Notes |
|---|---|---|---|
| 0 | Grill-me design (10 Q, 2 rounds) | ✅ DONE 2026-08-21 | service account / both outputs / one-liner / on-demand+range picker / preview-every-diff / auto-append / TRAFFIC sub-tab / app full parity |
| 1 | Brief + checkpoint written | ✅ DONE | this file + TN-DAILY-TRAFFIC-ADW-BRIEF.md |
| 2 | Railjack baseline gates | ✅ DONE | 414 pytest + tsc green; pyjwt 2.13 installed in .venv |
| 3 | Hub op build (agy) | ✅ SHIPPED `f8216ea` | gates green; hub restarted, endpoint live (503 hint until ga.json) |
| 4 | Hub op validator | ✅ PASS (after host fix) | year-boundary append blocker + never-green assert fixed; append cap raised to 92 |
| 5 | Android app tn-daily-traffic (agy) | ✅ SHIPPED `b2fb5bd` | 17/17 tests (host-verified); tsc+vite+cap copy green; no creds in git (verified) |
| 6 | App validator | ✅ PASS (after correction round) | parity re-diff clean both directions; NITs: cellInt `+n` edge, pre-contract throw |
| 7 | Live verify + APK | ✅ LIVE-VERIFIED (test copy) · ⏳ APK pends Ben | GA live ✅ (232,575/232,911) · analyze+apply verified on TEST COPY (row 268/269 written, formulas untouched) · real-layout rewrites both sides, parity 5/5 PASS · hub `c2d2b32`, app `c0a71dd` · production cutover = Ben shares REAL doc to SA + one-line sheet_id swap · APK build (`npm run apk`) after cutover so the embed targets the real sheet |
| 8 | Vault: project note + hot.md + memory-log + Somatic handoff | ✅ DONE | note `20-projects/thailandnow-daily-traffic-op.md`; Somatic port left to Tasai via hot.md |

## Key facts (compact-proof)

- GA: account `365894319`, property `501926474`, metric **totalUsers**, per-date value = total users for range **2025-12-05 → date** (NOT summed dailies; GA dedups across days). TZ Asia/Bangkok.
- Sheet: "Analytics & Boosting", tab `2025-26 Web Traffic Report`, cols A–E `Date | Day | Target Traffic | Actual Traffic | Daily Traffic`; Day = days since 2025-12-05 (+1); Daily E = D(today)−D(yesterday); rows pre-made to Sep 3; Aug 13+ hold STALE 228,460/+0 → backfill must offer overwrite via preview-confirm. E may be formula → detect via values.get?valueRenderOption=FORMULA, write E only when no formula.
- One-liner text: `Aug 21 · Day 260 · Total 232,299 · Daily +1,021 · Target 211,258 (Δ +21,041)` (per day, newline-joined for ranges).
- Config: `~/.config/railjack/ga.json` `{client_email, private_key, property_id, sheet_id, tab, contract_start}`; missing → endpoints 503 + UI hint, COPY TEXT still impossible without GA (GA is the source) so whole tab shows config hint.
- New dep: `pyjwt[crypto]` (service-account JWT). Scopes: `analytics.readonly` + `www.googleapis.com/auth/spreadsheets`.
- Hub endpoints: POST `/api/thailandnow/traffic/analyze` {from,to} (read GA+sheet, return diff preview + text, NO write) · POST `/api/thailandnow/traffic/apply` {writes[]} (commit). UI: TRAFFIC sub-tab, from/to pickers default today.
- Panel note: hub restart + fresh report practice applies at ship (practices.md §coding).
- App: full parity, embedded SA key (read-only GA + sheet write), internal APK (Ben+Nat). WebCrypto RS256 JWT, no backend.

## Resume instruction

Read this file + the brief; continue at the first ⏳/🔒 phase. Gate commands:
`.venv/bin/pytest -q` · `cd frontend && npx tsc --noEmit && npm run build` · `.venv/bin/ruff check` · `bash scripts/check-free-first.sh` · `.venv/bin/python -m app.thailandnow`.
