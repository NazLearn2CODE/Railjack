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
| 3 | Hub op build (agy) | ✅ BUILT | 660 insertions, all gates green (414 pytest · tsc · build · self-check · ruff=baseline · free-first) |
| 4 | Hub op validator (independent subagent) | 🔁 FAIL→FIXED (host) | blocker: year-boundary append (clash-scan reordered after append branch) + never-green self-check assert + 92-dates alignment; regression assert (f) day-366 added; all gates re-green. NITs accepted+documented: text/append target mismatch, daily asymmetry edge, C-formula blanking, silent cap truncation, frontend day-count summary. Re-verify: next validator pass |
| 5 | Android app tn-daily-traffic (agy) | ✅ BUILT | repo created; 10/10 node tests (host-verified); tsc+vite+cap copy green; APK deferred (ga.json pending) |
| 6 | App validator | 🔄 RUNNING | dispatched 11:10, cross-parity vs hub `_traffic_proposed_writes` focus |
| 7 | Live verify | 🔒 BLOCKED on Naz | (a) GA service account JSON → `~/.config/railjack/ga.json`, (b) Ben shares "Analytics & Boosting" sheet to SA email |
| 8 | Vault: project note + hot.md + memory-log + Somatic handoff | ⏳ | |

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
