# RADIO ▸ News Fill — build progress (compact-safe checkpoint)

Contract: `RADIO-NEWS-BRIEF.md` (backend Task A, frontend Task B). Frontend IDE brief:
`RADIO-NEWS-IDE-BRIEF.md`. Approved plan: `~/.claude/plans/hello-there-gorgeous-we-clever-otter.md`.

## DONE + verified
- **Scout skill** `radio-news-scout` — live + vault, byte-identical. (writes handoff JSON)
- **Frontend (Task B)** — `frontend/src/components/NewsroomPanel.tsx` (Antigravity). BUILD-VERIFIED
  by host: `tsc && vite build` exit 0; all 4 endpoints + contract fields wired; Document Generator
  UI intact. **Live click-through still pending** (needs backend live on :8700).
- **Backend skill script (Task A §1)** — `newsroom/scripts/radio_news.py`, deployed BYTE-IDENTICAL to:
  - `/home/NAZ/Cephalon/10-knowledge/skills/newsroom/scripts/radio_news.py` (vault canonical)
  - `/home/NAZ/.claude/skills/newsroom/scripts/radio_news.py` (live)
  Built by GLM-5 (agent-x, sandboxed — staged in-project, host copied across the boundary).
  Host-verified: import OK; build_slotmap all 4 cases exact (10/7/10/6); locate_slots collision +
  idempotency + label-anchor sanity-tested; slice_leadin_fill OK.
  **Host fixes applied on top of GLM-5 output:**
  - `google_token()` rewritten to mirror `radio.py` exactly (unconditional refresh, uses file's
    `token_uri`, NO write-back to the shared credential file — GLM-5 had invented an `expires_at`
    cache that mutated `~/.config/railjack/google_token.json`, which the Document Generator reads).
  - `HEAD_RE` widened to `^(\d+)\.\[(.*)\]$` so already-filled slots are detected and reported
    `"already filled"` (was `"slot not found"`; the `empty` flag had been dead code).
  - removed now-unused `import time`.

## Backend Run 2 (Task A §2 + §3) — ✅ DONE + host-verified
- **`app/radio_news.py`** (NEW, 106 lines) — GLM-5 (agent-x `-P zai`), host-inspected. Exact
  `newsroom.py` mirror: `_run`/`_json`/`_fail`/`_script` with `_run`/`_script` extended to take
  optional `stdin: bytes|None` (PIPE + `communicate(stdin)`). Routes: `GET …/news/docs` (opt
  `?parent`/`?limit`), `GET …/news/report`, `POST …/news/apply` (400 on missing
  doc_id/kind/category; `json.dumps({"pieces":…,"slice":…})` → child stdin; timeout=180).
  No `shell=True`; argv lists only. Constants `SCRIPTS`/`RNEWS`/`PY` correct.
- **`app/main.py`** — +2 lines only (import + include_router after newsroom). Diff verified clean.
- **`tests/test_newsroom.py`** — +7 tests via `_rn_client` (monkeypatches `radio_news._run`,
  captures argv+stdin) and `_load_radio_news()` (importlib by path, skips if off skill path):
  build_slotmap sizes 10/7/10/6; locate_slots label-anchor + `("Global",1) not in slots`
  collision guard; slice lead-in; report happy+`_fatal`→400; docs argv+`--limit`; apply 400×3 +
  argv/stdin round-trip. **Host-verified: `.venv/bin/python -m pytest tests/test_newsroom.py -q`
  → 24 passed** (17 existing + 7 new, 0 skip — pure-fn tests reached the deployed script).
  NOTE: system `python3` has NO pytest — use `.venv/bin/python -m pytest`.

### radio_news.py public API surface (for the Run 2 brief / test authoring)
- `build_slotmap(category, kind) -> list[(tab,int)]`  (pure)
- `locate_slots(paragraphs) -> {(labelPrefix:str, num:int): {"bracket_index","body_index","empty"}}`
  where each paragraph = `{'startIndex', 'paragraph': {'paragraphStyle': {'namedStyleType'},
  'elements':[{'startIndex','textRun':{'content'}}]}}`  (pure)
- `slice_leadin_fill(text, title, source) -> str`  (pure)
- `process(piece) -> (title, body)`  pass-through seam
- subcommands: `list-docs [--parent][--limit]`, `report [--path]`, `fill --doc --kind --category` (stdin JSON)
- `_fatal(msg)` prints `{"_fatal":msg}` + exit 1.

## Task #9 — host-only end-to-end verify — ✅ DONE (2026-07-28)
Live-verified against throwaway COPIES of real weekday/weekend script docs (never touched a
production doc; scratch copies deleted after). Hub restarted; all 4 endpoints live (`docs`→200,
`report` normalize, `apply` 400-guard). **The live gate caught 3 real bugs the 26 unit tests
were blind to — all fixed in the skill script + regression-tested:**
1. **Wrong tab-body path** — read `t["body"]["content"]` (empty for tabbed docs) instead of
   `t["documentTab"]["body"]["content"]` → every slot "not found" → silent no-op. Extracted pure
   `tab_bodies(doc)` helper + `test_rn_tab_bodies_documenttab_path`.
2. **`token` variable shadowing** — the Slice lead-in loop `for token, repl in (...)` clobbered the
   OAuth `token` → final `batchUpdate` sent `"[ARTICLE HEADLINE]"` as the bearer → **401**. Only hit
   the weekday-global path (only one running the lead-in), so every other probe passed. Renamed loop
   var → `needle`.
3. **Malformed writes** — `deleteContentRange` used bare `start`/`end` (Docs wants
   `range:{startIndex,endIndex}`) → **400**; and NO request carried `tabId`, so MIDDAY/EVE writes
   would land in the AM tab's index space. Added tab-scoped `_ins`/`_del` builders threading `tab_id`;
   `tab_bodies` now returns `{title:{tab_id,paras}}`. `test_rn_request_builders_tab_scoped`.

**Live results (all correct):** weekday global = 10 hard (AM 1-3 / MIDDAY 1-4 / EVE 1,3,5) + Slice
slot 4 + lead-in `…we have <headline> from <source>.`; weekday business = 10 (AM/MIDDAY 2-5 / EVE 3,5),
Local-Biz slot 1 reserved empty; weekend global = 7 (MIDDAY 1-4 / EVE 1,3,5) with **AM tab untouched**;
weekend business = 6 (MIDDAY 2-5 / EVE 3,5), AM untouched. Idempotent re-run → 11 skipped
"already filled", zero double-insert. Styles inherit (title HEADING_2, body NORMAL_TEXT; no
`updateTextStyle` sent). Skill script byte-identical vault + `~/.claude` (md5 `a9eba8de…`). 26 tests pass.

NOTE: freshly Drive-copied docs 401 on `batchUpdate` for ~30-60s while ACLs propagate — irrelevant in
prod (Document Generator makes the docs days ahead); only bit the scratch-copy test flow.

## Doc picker revision (2026-07-28) — flat dropdown → folder browser
Naz: the flat 60-doc `<select>` was unusable. Replaced with a **one-level folder browser**
(RT 2026 ▸ month ▸ day), his pick over URL-paste / month+day dropdowns.
- **Backend:** new `browse` subcommand (`split_children` pure helper partitions a folder's children
  into sorted subfolders + NAME_RE script-docs, one Drive call per level, no recursion) +
  `GET /api/newsroom/radio/news/browse?parent=`. `list-docs` kept (harmless). Live-verified on
  `:8700`: root→13 folders/0 docs, September→30 docs newest-first, Daily Recordings→0 script-docs.
- **Frontend:** `selectedDocId`+flat `newsDocs` → `browseStack` breadcrumbs + `selectedDoc` object;
  drill via `enterFolder`, back via `jumpToCrumb`; ⟳ reloads level. `tsc`+`vite` exit 0.
- **Tests:** +2 (`test_rn_browse_argv_and_parent`, `test_rn_split_children_partitions_and_sorts`) → **28 pass**.
- Skill byte-identical vault + `~/.claude` (md5 `987d9ced…`).

## Remaining
- **Human gate only:** browser panel click-through (browse → pick doc → SCOUT → CONVERT → tick →
  CONFIRM) on a real doc — can't be host-driven. Backend + all 5 endpoints proven; frontend
  build-verified (tsc+vite exit 0).

## Refinement backlog (deferred — build when quota allows; Naz 2026-07-28)
Requested after the folder browser shipped + the panel worked live end-to-end. NOT started.
1. **Trim scout volume** — gather **15** pieces per category (down from ≥20) + **3** Slice-of-Life
   (down from 5). Cost driver = fetch+clean every article for the ≥190-word gate → fewer = cheaper.
   Still covers fill (weekday 10 / weekend 7 hard; slice needs 1). **Confirm:** "segment" = per
   category-run (one `global`, one `business`), NOT per broadcast.
2. **SEA quota** — ≥**1 Southeast-Asia piece per broadcast** (AM/MIDDAY/EVE), **≤3/day**. Placement
   rule, both sides: scout tags region + guarantees ≥3 SEA in pool; fill reserves one global slot
   per tab for SEA, rest rank newest-first. **Open Qs:** which slot per tab; weekend skips AM →
   2 broadcasts → 2 SEA not 3.
3. **Search method: unchanged** (Naz's hard line).

Tawhan's suggestions (Naz invited; unadopted, decide at build):
- **Cross-outlet dedupe** — drop near-dup stories (CNN+BBC same event) in the scout so one story
  doesn't eat two slots. Cheap; quality win. → **ADOPTED + shipped (scout L1).**
- **Lazy body fetch** (bigger saver, trade-off) — scout returns title/url/snippet/date only; fetch
  full ≥190w body at APPLY time for ticked pieces only. Saves ~5 wasted fetches/run but adds a
  re-fetch (paywall risk) + breaks the "scout ships content, no re-fetch" design. Reserve for if #1
  alone doesn't fix the bill.

## Cheap lane + SEA fill — DONE + host-verified (2026-07-28)
Two features Naz added on top of the refinements, both for max token economy. **Backend shipped
+ tested (33 pass); frontend (2 buttons) built by host + build-verified — Naz: "I LOVE IT".**

### SEA-lead placement (#2 fill layer 2 — the engine)
- New pure `assign_pieces(slotmap, pieces, category)` in `radio_news.py`: global runs reserve
  **GLOBAL slot 1 of each broadcast** (AM/MIDDAY/EVE) for a `region:"SEA"` piece; remaining slots
  take the rest newest-first. Graceful fallback both ways (short SEA pool → slot 1 falls back to
  non-SEA; surplus SEA spill into open slots). Business = sequential, region ignored.
- `cmd_fill` refactored → shared `_fill_doc(token, doc_id, kind, category, pieces, slice_piece)`
  used by **both** `fill` (stdin) and the new `autofill`. `written[]` entries now carry `region`.
- Smoke-proven: weekday-global → SEA at AM1/MIDDAY1/EVE1, g0..g6 into the 7 others; weekend 1-SEA
  fallback correct; business sequential.

### CHEAP SCOUT (scout exact-count mode)
- Scout `SKILL.md` gains an **exact mode**: any of `--results N --sea M --slice K` ⇒ gather exactly
  those counts, stop the moment each quota is met (no headroom). Pairs with AUTOPILOT.
- Panel computes N/M/K from category+kind: weekday global `10/3/1`, weekend global `7/2/0`
  (no AM → no Slice), weekday business `10/0/0`, weekend business `6/0/0`. Injects
  `/radio-news-scout <cat> --results N --sea M --slice K` into the terminal (reuses the SCOUT path).

### AUTOPILOT (one-click convert+place+fill)
- New `autofill` subcommand: reads the handoff itself (no stdin), dedups `results`, picks slice
  (weekday-global only), guards category mismatch (`_fatal` if handoff cat ≠ requested), calls
  `_fill_doc`. Deterministic placement = **no LLM in the loop** = the real token save.
- Route `POST /api/newsroom/radio/news/autofill {doc_id,kind,category}` (no stdin, 180s). Live-
  verified: empty body → **400** guard, route loaded (not 405) after `systemctl --user restart`.
- Idempotent (skips already-filled slots), same as manual fill.

### Frontend — DONE (host-built, not Antigravity, 2026-07-28)
- `NewsroomPanel.tsx` gains a **CHEAP LANE** block under the folder browser (shares doc + category
  with the curated flow): **CHEAP SCOUT** injects `/radio-news-scout <cat> --results N --sea M
  --slice K` (N/M/K from the count table, caption shows the exact numbers), **AUTOPILOT** POSTs
  `…/news/autofill {doc_id,kind,category}` and renders through the APPLY result panel. Result header
  reads `AUTOPILOT FILLED (N picked)` vs `APPLIED`; SEA-led slots get a `SEA` badge. Both disabled
  until a doc is picked. `tsc --noEmit` + `vite build` exit 0.

### CP2 — glm-5 Ben-voice rewrite (DONE + host-verified, 2026-07-28, `bfa0ef1`)
- Rewrite seam in `radio_news.py`: `_rewrite`/`_gateway`/`_parse_rewrite`/`_prime_rewrites` — every
  assigned piece is rewritten via the OmniRoute gateway (`naz-backup`) into a <=250-word broadcast
  cut **before** the doc is read (fail-fast: gateway down ⇒ 502, doc untouched, no half-fill).
- Short-circuits: `rewritten:true` (Antigravity ultra-cheap seam) + `RADIO_REWRITE=off` (offline
  tests) make zero gateway/gem contact. `strict=False` parse (body carries literal `\n`).
- Gem `app/gems/radio-news-rewrite.md` (Editor Ben's voice); app wrapper passes `RADIO_REWRITE_GEM`.
- 41 tests green; live seam verified end-to-end (sample article → 88w broadcast cut, no subheads).

### CP3 — publication-formatting pass (DONE + host-verified, 2026-07-28)
- NEW reusable script `newsroom/scripts/doc_format.py` (stdlib urllib): bolds **people names**
  (glm-5 `app/gems/doc-format-entities.md`) + underlines **dates** (regex: month-day / day-month /
  ISO). One tab-scoped `batchUpdate` per tab; idempotent. Gateway-down **degrades** (dates still
  underlined, names skipped, 200 + `names_skipped:true`) — NOT fail-fast, per the gem's contract.
- Route `POST /api/newsroom/format/apply {doc_id, tab?}` (120s). FORMAT in the News Fill status
  row: a standalone button for re-runs/any doc, AND **auto-chained after every successful APPLY +
  AUTOPILOT** (so a fill is one click — fill writes, then format bolds names + underlines dates).
  `tsc + vite build` exit 0. 52 tests green.
- Known quirk (pre-existing, shared `_script` wrapper, NOT CP3): a `_fatal` exits nonzero so the
  `rc!=0 → 502` branch beats the `_json → 400` path — script config/validation errors surface as 502,
  not 400. Message is preserved either way; flagged, not fixed (out of scope).

### Pending
- **Human gate (Naz):** live end-to-end on a real weekday doc — CHEAP SCOUT → AUTOPILOT → FORMAT,
  eyeball the broadcast copy + bold/underline polish. Can't be host-driven.
- **Known:** the `_script` 502-vs-400 quirk above (cosmetic; fix when a client needs the distinction).

### Backlog — ULTRA CHEAP mode (after the rewrite; Naz, 2026-07-28)
- Offload the scout itself off the metered Claude session: send the scout **or** cheap-scout
  instruction to **Antigravity IDE**, which does the scouting + writes the handoff JSON that
  CONVERT/AUTOPILOT already consume (same `/tmp/railjack-radio-news/latest.json` report shape).
  Net effect: zero scouting tokens on the Claude side. Sits behind the content-rewrite big fish.
