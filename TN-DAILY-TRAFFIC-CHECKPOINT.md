# Daily Traffic OP — BUILD CHECKPOINT (compact/resume anchor)

Updated: 2026-08-21T23:20+07 · Owner: Tawhan (home) · Status: **LIVE-VERIFIED, APK pending cutover**

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
- Sheet (REAL layout, verified by live read 2026-08-21): "TN Analytics & Boosting", tab `2025-26 Web Traffic Report`. Rows 2–6 = phase metadata (ignored). **Header row ~9** (detected by "Day" label, never hardcoded). Data rows: **A empty; B = serial date int** (45996 = 2025-12-05 = Day 1; epoch 1899-12-30); **C = Day** (primary key); **D = Target = FORMULA always** (`=$I$3*C10` early, `=D267+$I$6` later); **E = Actual = plain number — THE ONLY COLUMN EVER WRITTEN**; **F = Daily = FORMULA always** (`=E11-E10`). Writes echo full row A–F with D/F verbatim; appends shift D/F formulas by pattern (`^=D(\d+)\+(\$[A-Z]\$\d+)$`, `^=E(\d+)-E(\d+)$`, gap-preserving); unshiftable → numeric fallback/warning. Text targets need a second `UNFORMATTED_VALUE` read (D is a formula). Optional target/daily columns degrade gracefully (guard both sides).
- Live state 2026-08-21 evening: SA `tn-traffic-op@thailandnow-project.iam.gserviceaccount.com` (Naz's personal project `thailandnow-project` — org policy blocked the company one; APIs Analytics Data + Sheets enabled; GA property Viewer granted via Work Naga askworknaga@gmail.com Org Admin). `~/.config/railjack/ga.json` currently points at **TEST COPY** `1B0slJoL7C9Xow8cUraFErTPDAzoBbgJoCui78CmPkUs` ("Copy of TN Analytics & Boosting FOR NAZ TESTING", in Naz's shared-with-me). REAL doc id `117ToHtWxtbTcZCxGqO07Qcu1bqMxVGhnrLRRo72hRh8` — DO NOT write it without Naz's explicit go. GA verified live: 232,575 (Aug 20) / 232,911 (Aug 21); sheet's Aug 20 was 232,587 (GA retro-dedup, preview shows the diff, Naz decides). nat fills the sheet daily (~row 268 = Aug 20).
- One-liner text: `Aug 21 · Day 260 · Total 232,299 · Daily +1,021 · Target 211,258 (Δ +21,041)` (per day, newline-joined for ranges).
- Config: `~/.config/railjack/ga.json` `{client_email, private_key, property_id, sheet_id, tab, contract_start}`; missing → endpoints 503 + UI hint, COPY TEXT still impossible without GA (GA is the source) so whole tab shows config hint.
- New dep: `pyjwt[crypto]` (service-account JWT). Scopes: `analytics.readonly` + `www.googleapis.com/auth/spreadsheets`.
- Hub endpoints: POST `/api/thailandnow/traffic/analyze` {from,to} (read GA+sheet, return diff preview + text, NO write) · POST `/api/thailandnow/traffic/apply` {writes[]} (commit). UI: TRAFFIC sub-tab, from/to pickers default today.
- Panel note: hub restart + fresh report practice applies at ship (practices.md §coding).
- App: full parity, embedded SA key (read-only GA + sheet write), internal APK (Ben+Nat). WebCrypto RS256 JWT, no backend.

## Resume instruction

Continue at the ⏳ items of phase 7, in this order:
1. **Cutover** (pends Ben): he shares REAL doc `117ToHtWxtbTcZCxGqO07Qcu1bqMxVGhnrLRRo72hRh8` to the SA email as **Editor** → host swaps `sheet_id` in `~/.config/railjack/ga.json` to the real id → one live analyze+apply (confirm with Naz first; preview will show nat's Aug 21 number vs GA final).
2. **APK**: in `~/Coding Projects/tn-daily-traffic` run `npm run apk` (embed reads ga.json — must point at REAL sheet first; SDK `$ANDROID_HOME=/home/NAZ/.local/share/android-sdk`); verify no creds in `git ls-files`; hand APK to Ben + Nat.
3. Close out: checkpoint commit; vault hot.md/memory-log touch if anything material changed; goal-865f5e4f already complete.

Gate commands: `.venv/bin/pytest -q` (414) · `cd frontend && npx tsc --noEmit && npm run build` · `.venv/bin/ruff check` (38 baseline) · `bash scripts/check-free-first.sh` · `.venv/bin/python -m app.thailandnow`. App: `npm test` (21/21) · `npm run build` · `npm run apk`.
Subagent registry (DSH, idle/ready): hub builder 1499c036 · app builder e22c6f6f · hub validator 9dc6e19b · app validator 2331f99a (round-3 PASS) · 33898535 (un-orphan) · 3146fcd6 (Somatic port).
