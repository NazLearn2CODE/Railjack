# BRIEF — tn-daily-traffic: standalone Android app (Daily Traffic OP, full parity)

New repo: `/var/home/NAZ/Coding Projects/tn-daily-traffic` (create it). You are a
single-turn builder: implement EXACTLY this brief. Reference repos (READ them, never
byte-copy — disjoint lineages):
- `/var/home/NAZ/Coding Projects/helios` — Capacitor+React+Vite+TS standalone-app pattern,
  esp. `scripts/make-embed.mjs` + `src/lib/embed.ts` + vite `define` obfuscated-creds
  mechanism, `package.json` scripts (`apk` lanes), `android/` gradle setup.
- `/var/home/NAZ/Coding Projects/Railjack/frontend/src` — dark-HUD console design language
  (borders/mono/labels/colors) to port visually.
Companion checkpoint: Railjack `TN-DAILY-TRAFFIC-CHECKPOINT.md`.

## What the app does (settled design)

For Ben/Nat, on their phones, with ZERO backend: pick date range (default today) → RUN →
GA4 cumulative Total Users (contract start 2025-12-05 → each date) + live sheet state →
diff preview (OLD→NEW) → **COPY TEXT** (one-liner per day) and/or **WRITE SHEET**
(confirm-gated). Full parity with the hub op.

## Facts (same as hub brief)

- GA: property `501926474`, metric `totalUsers`, per-date runReport
  (`https://analyticsdata.googleapis.com/v1beta/properties/{id}:runReport`, body
  `{"dateRanges":[{"startDate":contract_start,"endDate":date}],"metrics":[{"name":"totalUsers"}]}`).
- Sheet: id/tab/config come from the SAME `~/.config/railjack/ga.json` shape
  `{client_email, private_key, property_id, sheet_id, tab, contract_start}`; embed at
  build time via the make-embed mechanism (obf embed, gitignored, NEVER committed).
- Auth: service-account JWT RS256 signed in-app with **WebCrypto**
  (`crypto.subtle.importKey("pkcs8", …, {name:"RSASSA-PKCS1-v1_5", hash:"SHA-256"})`),
  claims `{iss, scope, aud:"https://oauth2.googleapis.com/token", iat, exp:iat+3600}`;
  exchange at the token endpoint. Scopes: `analytics.readonly` +
  `https://www.googleapis.com/auth/spreadsheets`. Cache token until exp. One scope-joined
  token request (space-joined scopes) is fine.
- Sheet read: `GET …/values/{tab}!A1:E5000?valueRenderOption=FORMULA`; writes: per-row
  `PUT …/values/{tab}!A{row}:E{row}?valueInputOption=USER_ENTERED` + one
  `POST …/values/{tab}!A1:append?valueInputOption=USER_ENTERED` for appended rows.
- Day = days since 2025-12-05 (+1); Daily = Actual(today)−Actual(yesterday); E written
  only when existing cell is not a `=`-formula; past-last-row dates → append rows
  (Target copied from last plain-number C, else blank+warning). Match rows by Day column
  (B) primarily; mismatch → warning+skip. TZ Asia/Bangkok (`Intl.DateTimeFormat` with
  timeZone, no libs).
- Text one-liner per day:
  `{Mon D} · Day {n} · Total {t} · Daily {+d} · Target {target} (Δ {±gap})`
  (numbers with thousands separators `,`; Daily 0 → `+0`; missing target → omit Target
  and Δ parts). Lines joined by `\n`.
- Caps: ≤92 days per RUN.

## Build

1. Scaffold: `npm create vite@latest . -- --template react-ts` in the new repo, add
   `@capacitor/core @capacitor/cli @capacitor/android`, `npx cap init "TN Traffic"
   app.tn.traffic --web-dir dist`, `npx cap add android`. No UI frameworks beyond React —
   port the HUD look with plain CSS (mono font, bordered panels, dark bg) from Railjack.
   No other deps.
2. `src/lib/gaConfig.ts` — decode embedded config (obf pattern from helios; vite `define`
   injects; `.gitignore` the embed artifact; `scripts/make-embed.mjs` reads
   `~/.config/railjack/ga.json` on the BUILD machine — if absent, build fails loudly).
3. `src/lib/googleAuth.ts` — WebCrypto JWT + token cache (above spec).
4. `src/lib/traffic.ts` — PURE functions, dependency-free, exported for tests:
   `trafficDay`, `trafficDates` (92 cap), `trafficTextLines`, `proposedWrites`
   (same semantics as hub: writes/appends/warnings, formula-E detection, Day-match).
5. `src/lib/sheets.ts` — read/write via `fetch` (scope above). Surface HTTP errors with
   readable text.
6. `src/App.tsx` — single screen: from/to date inputs (default today Asia/Bangkok),
   RUN (busy/error states), preview list OLD→NEW (append rows marked NEW ROW, warnings
   amber, formula rows note `E=formula, untouched`), COPY TEXT (navigator.clipboard),
   WRITE SHEET → inline confirm → result summary (`✓ n written · m appended` +
   failures). Missing-embed build path must not crash at runtime — show config hint.
7. `npm run build` = `tsc -b && vite build && npx cap copy android`; `apk` lane =
   make-embed → build → `cd android && ./gradlew --no-daemon assembleDebug`.
   ANDROID_HOME is set on this machine; gradle wrapper comes from `npx cap add android`.
8. Tests: a tiny `npm test` script running `node --test` (or vitest if already dev-dep —
   it is NOT, so use `node --test` + tsx? keep zero-dep: write the pure-function tests as
   a plain `tests/traffic.test.mjs` importing compiled-free pure JS mirror? NO — simplest
   zero-friction: `src/lib/traffic.test.ts` compiled by `tsc -b` into the build, and a
   `node --test dist/` lane; if that fights you, one `scripts/selfcheck.mjs` that
   re-implements nothing but imports the .ts via a one-off esbuild? DO NOT add esbuild —
   vite already ships it as a transitive; `node --experimental-strip-types --test
   src/lib/traffic.test.ts` (Node ≥22 here) is the lane. Verify node version first.)
   Cover: trafficDay (Dec 5→1, Aug 21 2026→260), 92-cap, exact text line, formula-E skip,
   append with target copy, Day-mismatch warning.
9. README.md: what it is, build steps (embed → apk), where creds come from, internal-use
   only (2 people), obf ceiling note (same ponytail wording as helios).

## Hard constraints

- No backend, no telemetry, no new runtime deps beyond Capacitor+React. Key never in git,
  never in logs, never in error text.
- Implement natively; reference repos are for reading. Reuse helios's embed CONCEPT, not
  its files.
- The pure module (`traffic.ts`) must be byte-identical in SEMANTICS to the hub's
  `_traffic_proposed_writes` (same inputs → same outputs); if you find a bug in the spec,
  note it in NOTES, don't silently diverge.

## Report (exactly)

```
FILES CREATED: <tree summary, one line per area>
APK: <built? path | not built, why>
TESTS: <node --test result summary | not run, why>
NOTES: <ambiguities/spec bugs found, or "none">
```
