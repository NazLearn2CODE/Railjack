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

## Remaining
- **Human gate only:** browser panel click-through (SCOUT → CONVERT → tick → CONFIRM) on a real doc —
  can't be host-driven. Backend + all 4 endpoints proven; frontend build-verified (tsc+vite exit 0).
- Ship: commit Railjack + vault (skill fix) + module note + vault-check + memory-log.
