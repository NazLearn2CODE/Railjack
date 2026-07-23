# Thailand NOW — Railjack module (portable to Somatic)

## Context

Naz runs a monthly **Thailand NOW** content pipeline across three "desks" (writers/streams):
Paul (12 articles), Teerin (5 blogs), and TIAN (event write-ups). Today each cycle means
hand-creating dozens of Google Docs in the right Drive folders, Trello cards on the right
lists, and attaching one to the other — repetitive, error-prone, and fixed to these exact
writers/counts/locations. He also wants an **Events Radar** that scouts upcoming Thailand
events (business seminars, cultural activities — cf. thailandnow.in.th/events) and, for a
chosen event, generates a ready-to-paste publicity bundle and spins up the doc+card pair.

This plan adds **one** Railjack panel module — `thailandnow` — with two tabs (WRITERS, EVENTS)
sharing a single **desk-driven create/attach engine**. The desk abstraction (Paul/Teerin/TIAN
are just config rows) is what makes the system monthly-reusable and writer-agnostic, and what
makes the Events Radar's "create" action just another desk invocation. The module follows
existing Railjack conventions so it ports to Somatic by copying code + swapping machine-local
YAML + creds.

**Why one module, not two:** WRITERS bulk-setup and EVENTS radar share the exact same backend
primitive (create-doc-in-folder → create-card-in-list → attach-doc-to-card) and the same
external auth (Google + Trello). Splitting would duplicate that plumbing. One module, two tabs,
shared engine — fewer files, one auth story, one port.

## Confirmed product rules (from Naz)

- **`[CAT]` is a literal placeholder.** Doc/card names keep the string `[CAT]` verbatim; the
  editor (Ben) fills the real category himself later. No category selector, no token resolution.
- **Articles & Blogs (Paul/Teerin) docs are blank** — title only, writers fill everything.
- **Event (TIAN) docs are prefilled** with an AI-generated publicity bundle (see Events flow),
  produced from the event's URL(s) via the Thailand NOW Event Publicity gem. The Trello card
  description carries the event URL(s) + any related-image links; the Doc is attached to the card.

## Design system (per `/frontend-design` + `/home/NAZ/open-design/`)

`open-design` is a standalone agent-native product — **not** imported by Railjack. Railjack has
its own theme, the dark **"Orbital Telemetry Console"** (spec: `20-projects/railjack-design-language.md`,
impl: `frontend/src/index.css`; phosphor-cyan `#38e0ff` on `#070a0f`, Chakra Petch + IBM Plex Mono).

We apply the spirit of both references **within** Railjack's existing theme:
- **Consistency wins** — reuse `index.css` primitives so the module reads as part of the cockpit.
  Primitives: `.hud`/`.hud--bracket`/`.reveal`, `.btn`/`.btn--signal`/`.btn--crit`/`.btn--compact`,
  `.input`, `.label`, `.pip--*`, `.row-in`, `.caret`, the shared **JOBS card** pattern.
- **Layout** — mirror `NewsroomPanel`'s two-tab pattern (WRITERS / EVENTS, active = `.btn--signal`),
  each tab single-column stacked HUD cards (the Comfy/FFmpeg convention). The Events "thick box"
  is a prominent processing card that replaces the RESULTS list when an event is picked.
- **open-design craft rules** — full 5-state coverage per card (loading / empty / error /
  populated / edge), anti-AI-slop (no indigo accents, no emoji-as-icons, real copy), form
  validation, loading thresholds (spinner `<2s`, label `>2s`).
- **frontend-design placement** — bold bracket-HUD hierarchy; the "one unforgettable thing" is
  the live JOBS feed showing docs+cards (and, for events, the generated bundle) materializing.

## Architecture (the 6 touch points — from scout 1)

Repo: `/var/home/NAZ/Coding Projects/Railjack/` · FastAPI + uvicorn (systemd `:8700`) · Vite+React+TS+Tailwind v4.

| # | File | Change |
|---|------|--------|
| 1 | `configs/tawhan.yaml` | add `- id: thailandnow, kind: panel, panel: thailandnow` + `options:` (desks, board, publicity LLM, gem path) |
| 2 | `app/thailandnow.py` | **NEW** — `router = APIRouter()`, all endpoints |
| 2b | `app/gems/event-publicity.md` | **NEW** — the gem prompt (module-local copy; kept in sync with vault canonical `gemini-gem-thailandnow-event-publicity.md`) |
| 3 | `app/main.py` | `from .thailandnow import router …` + `app.include_router(…)` |
| 4 | `frontend/src/components/ThailandNowPanel.tsx` | **NEW** — two-tab panel |
| 5 | `frontend/src/App.tsx` | add `thailandnow: ThailandNowPanel` to the `PANELS` map |
| 6 | `frontend/` → `npm run build` | rebuild dist so FastAPI serves the new panel |

**No new Python deps.** Google/Trello/Jina/Pexels/Pixabay/LLM are all **httpx** calls (httpx
already in `pyproject.toml`). No `google-api-python-client`, no `py-trello` — ponytail: the APIs
are trivial REST; a thin in-module client is smaller and more portable than an SDK.

## The shared engine: the "desk" config model (core abstraction)

A **desk** = one writer/stream. Paul, Teerin, TIAN are three rows. Adding/removing a writer or
changing counts/locations = editing a YAML row. The create-engine reads desk fields, so it is
fully generic.

```yaml
# configs/tawhan.yaml → module options
- id: thailandnow
  kind: panel
  panel: thailandnow
  options:
    google_token_path: ~/.config/railjack/google_token.json
    trello_board_short: VTMuHmEj          # resolved to board id once (setup endpoint), cached
    trello_board_id: ""                   # filled by /api/thailandnow/setup
    publicity_llm:                        # drives the Event bundle generation
      base_url: "${ANTHROPIC_BASE_URL}"   # reuse the home z.ai/OmniRoute gateway already in .env
      model: glm-5                        # swap to gemini-* by pointing base_url at a Gemini endpoint + key
      api_key_env: ANTHROPIC_API_KEY
    gem_path: app/gems/event-publicity.md # read fresh each call (edits take effect without redeploy)
    desks:
      - id: paul
        kind: article
        drive_folder_id: 1w8ESeKzvW-Er36qjMjgfLU7fMwa8KSu6
        trello_list_name: "To draft (Paul)"
        doc_name:   "[{yyyymm}] [CAT] #{nn}"      # [CAT] literal — Ben fills
        card_name:  "Article | {mon} #{nn}"
        count: 12
      - id: teerin
        kind: blog
        drive_folder_id: 1jUTZ5qchpViAioBWfUpIdu5PQCwdSWSq
        trello_list_name: "To draft (Teerin)"
        doc_name:   "[{yyyymm}] [CAT] Working Title #{nn}"
        card_name:  "Blog | #{nn}"
        count: 5
      - id: tian
        kind: event
        drive_folder_id: 1_gJui6atpHTjaZWnkBNG6BwEgiZ2ZtUm
        trello_list_name: "To draft (TIAN)"
        doc_name:   '[{yyyymm}] [EN] "{title}"'
        card_name:  'Event | {title}'
        count: 1
```

**Template tokens:** `{yyyymm}` (202607), `{mon}` (JUL), `{nn}` (zero-padded per-desk-per-month
sequence), `{title}` (event name, events only). `[CAT]` is **not** a token — it is literal text
left in the name for Ben to replace.

**`{nn}` auto-increment without dupes:** before generating, scan the target Drive folder + Trello
list for existing names matching the template, take max `nn` seen for the current `{yyyymm}`, and
continue from there. Re-running mid-month never re-creates #01.

## Backend — `app/thailandnow.py`

Own `router = APIRouter()` (included in `main.py`), like newsroom. Unlike newsroom it does **not**
subprocess — it calls REST APIs directly with httpx (the comfyui/ffmpeg pattern). Endpoints:

1. `GET  /desks` → return the desk config (frontend builds the WRITERS tab + the TIAN create from it).
2. `POST /setup` → resolve `trello_board_short` → board id, each `trello_list_name` → list id
   (Trello `GET /1/boards/{id}/lists`), cache into config + YAML. One-time; also a panel button.
3. `POST /provision` `{desk_id, count?, yyyymm?, title?, body?, card_desc?}` → the shared
   create/attach engine. For each of N: resolve tokens (scan for next `{nn}`) → **create Google
   Doc** in `drive_folder_id` (Drive `files.create`, mime `application/vnd.google-apps.document`,
   `parents=[folder_id]`) → link-shareable (Drive `permissions.create`, `anyone/reader`) →
   **write body only if `body` is non-empty** (Docs `batchUpdate` insertText; Articles/Blogs pass
   no body → blank doc, title only — per Naz's rule) → **create Trello card** in the resolved list
   (`POST /1/cards`, `idList=`, `desc=` the passed `card_desc`) → **attach** doc `webViewLink`
   (`POST /1/cards/{id}/attachments`). Streams progress to the JOBS feed.
   Returns `[{doc_name, doc_url, card_name, card_url}, …]`.
4. `POST /events/scout` `{weeks?}` → Events Radar. Free-first via **Jina Reader** (keyless):
   `https://r.jina.ai/https://www.thailandnow.in.th/events` + DuckDuckGo-via-Jina broad query
   (`thailand events {month} {year} seminar conference culture`). Parse listings into
   `[{title, url, date, location}]`. Follows the vault web-research protocol.
5. `POST /events/publicize` `{event, urls}` → the gem step. Jina-fetch each URL → concatenate as
   "raw event information" → call `publicity_llm` with **system = the gem prompt** (read fresh from
   `gem_path`), **user = the raw info** → return the plain-text 5-part bundle (Facebook / X /
   Instagram / Meta Description / Long-form Article). The bundle is shown editable in the thick box
   before any doc is created — Naz reviews/tweaks, nothing is auto-committed blind.
6. `POST /events/images` `{event}` → related-image lookup. Tier 1: scrape the event's URL via Jina
   for embedded images (`<img src>` ≥1200px). Fallback **Pexels** + **Pixabay** by event name
   (keys from env). Returns ranked image URLs. (Chain documented in `f5-story-scout` BROLL mode.)
7. `POST /events/create` `{event, urls, bundle_text, image_urls?}` → calls the `/provision` engine
   with the **tian** desk, `title=event.title`, **`body=bundle_text`** (the reviewed publicity bundle
   becomes the doc content), `card_desc=` the event URL(s) + chosen image links. Same primitive as
   WRITERS — Events is just a desk invocation with a non-empty body.

**Auth helpers (in-module, from existing patterns):**
- **Google** — copy the refresh pattern from `~/.claude/skills/newsroom/scripts/nl_append.py:58-72`
  (`token()`): load `google_token.json`, POST refresh to `token_uri`, return access_token. Scopes
  minted = `documents` + **`drive`** (the newsroom token only has `drive.readonly`; creating in a
  folder needs `drive`). Token minted once via a small interactive script reusing `nl_auth.py`'s
  flow + the **already-production-published** OAuth app (project fast-reactor-465415-t9) → no
  Google review, no 7-day consent cap. Dedicated file `~/.config/railjack/google_token.json`
  (isolated from newsroom).
- **Trello** — key+token from env (`TRELLO_KEY`, `TRELLO_TOKEN`), query-param on every URL. Creds
  already exist (vault `.claude/settings.local.json` + n8n `.secrets.env`). API patterns in
  `10-knowledge/hermes/trello-card-contract.md` + `20-projects/n8n-trello-doc-to-wordpress-pipeline.md`.
- **LLM** — reuses the gateway already configured for Railjack's AI features
  (`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` in `.env`, or `ZAI_API_KEY` in `~/.config/railjack/env`).
  The gem is model-agnostic; default model `glm-5`, swappable to Gemini by retargeting `publicity_llm`.

## Frontend — `frontend/src/components/ThailandNowPanel.tsx`

Two tabs (`.btn--signal` active). Receives `{ module }`, reads desks via `usePolling('/api/thailandnow/desks')`.

**WRITERS tab** — single column:
- Card "DESK" — desk selector (`.input`), COUNT (default from desk config), month override
  (defaults to current `{yyyymm}`), `GENERATE` (`btn--signal`). (No category field — `[CAT]` is literal.)
- Card "PREVIEW" — resolved names for the first 2 items (catch template mistakes before commit).
- Card "JOBS" — shared JOBS-row idiom: pip + mono doc/card name + STATUS·N% + progress bar; each
  row links to the created Doc and Trello card. Empty: "no batches this month".

**EVENTS tab** — single column, two modes:
- **List mode** (default):
  - Card "SCOUT" — query/window inputs + `SCOUT` (`btn--signal`); loading = caret + "SCANNING…".
  - Card "RESULTS" — list rows (`.row-in`): pip + mono event title + muted date/location.
    Clicking a row enters **thick-box mode** for that event.
- **Thick-box mode** (the processing panel Naz asked for) — replaces RESULTS:
  - Card "EVENT" — title, date, location, the fetched URL(s) each with a use-toggle/checkbox
    ("use these URL(s)"); `BACK` to return to the list.
  - Card "PUBLICITY" — `GENERATE BUNDLE` (`btn--signal`) calls `/events/publicize` → the 5-part
    plain-text bundle in an **editable** `.input` textarea (review/tweak before commit). Loading:
    caret + "WRITING…". Empty: "generate a bundle or write your own".
  - Card "IMAGES" (optional) — `FIND IMAGES` (`btn--compact`) → thumbnail grid, each copyable;
    select which to include in the card.
  - `CREATE DOC+CARD` (`btn--signal`) → `/events/create` with the (edited) bundle + selected URLs/images → the shared JOBS feed.

## Portability → Somatic

Code is identical on both machines; only machine-local bits live in `options:` + env (scout 1):
- **Copied as-is:** `app/thailandnow.py`, `app/gems/event-publicity.md`, `ThailandNowPanel.tsx`,
  the `PANELS` entry, the `main.py` include.
- **Per-machine (in `configs/<machine>.yaml` + `~/.config/railjack/env`):** the `desks:` table
  (folder ids, list names, counts), `trello_board_short`, `google_token_path`, `publicity_llm`,
  and env keys (`TRELLO_KEY`, `TRELLO_TOKEN`, `PEXELS_API_KEY`, `PIXABAY_API_KEY`).
- Port = the hand-copy: `git checkout machine/somatic -- app/thailandnow.py app/gems frontend/src/components/ThailandNowPanel.tsx`,
  then add the YAML block + env keys on Somatic. Nothing in the code hardcodes a path, id, or cred.

## One-time setup (home first, then repeat per machine)

1. Mint the Google token: run the consent script (reuses `nl_auth.py` flow + published OAuth app,
   scopes `documents`+`drive`) → `~/.config/railjack/google_token.json` (mode 600).
2. Add to `~/.config/railjack/env` (systemd `EnvironmentFile`; `.bashrc` is **not** sourced by the
   unit): `TRELLO_KEY`, `TRELLO_TOKEN`, `PEXELS_API_KEY`, `PIXABAY_API_KEY`. Restart the unit.
3. `POST /api/thailandnow/setup` once (or the in-panel button) → resolve `VTMuHmEj` → board id and
   the three list names → list ids; cached into config.
4. `↻ CFG` to live-reload.

## Implementation slices

**Progress tracker** — tick a slice only after it's verified + committed. See **Safepoint & resume
protocol** below. A cheaper model (GLM via agent-x) can implement each slice from this spec; the
host verifies.

- [x] 1. **Scaffold + desks read** — files 1–6, `GET /desks`, empty two-tab panel. Verify: panel appears, desks render from YAML. ✓ 2026-07-24: live `/api/thailandnow/desks` returns paul/teerin/tian, ready; tsc+vite clean; service restart → route UP. Note: `_opts()` reads fresh per-request (reload-safe); Trello list/board ids resolved on-demand at slice 3 (no cached slots).
- [~] 2. **Google create-in-folder** — token helper + `files.create` + `permissions.create`. Verify: creates a Doc in the Paul folder, link opens, **body blank** (title only). ⧖ 2026-07-24: code done (`_google_token` refresh + `_google_create_doc` files.create+permissions+batchUpdate) and gated on a minted token; `app/tn_auth.py` written (documents+drive scopes; `uv run --with google-auth-oauthlib python -m app.tn_auth --client-id … --client-secret …`). Awaits Naz pasting the fast-reactor OAuth client → mint → verify.
- [x] 3. **Trello create+attach + `{nn}` dedup scan** — `/provision` end-to-end for Paul. Verify: 12 docs + 12 cards, doc attached to each, names `[202607] [CAT] #01..#12`, re-run yields #13+. ✓ 2026-07-24: Trello half + dedup verified live — list-id resolves case-insensitively (board lists are ALL-CAPS), create-card+attach+delete works, `/provision` resolves Paul's next **#13** (dedup sees the existing batch) then 503s at the Google gate with a clear message. The full 12-doc run verifies once the Google token mints.
- [ ] 4. **All three desks** — Teerin + TIAN via `/provision` (TIAN with a manual body to prove the path). Verify templates. **← module is usable end-to-end for WRITERS after this slice.**
- [x] 5. **Events scout + parser** — `/events/scout` via Jina. Verify: returns real events with URLs. ✓ 2026-07-24: live POST returns 6 real events (IMF–WBG Annual Meetings, Ubon Candle Festival, ULTRAMAN run, etc.) with clean `/event/<slug>` URLs; DDG is an optional `query`-triggered second source (aggregator-noise, off by default); parser self-check passes. Done ahead of 2/3/4 (blocked on Google+Trello creds).
- [x] 6. **Publicity bundle** — `/events/publicize` (LLM + gem + Jina fetch). Verify: a real event URL yields the 5-part plain-text bundle, THB-first, no markdown. ✓ 2026-07-24: live POST on ULTRAMAN HERO RUN → clean 4 KB 5-part bundle (type inferred Cultural; real reg URL in CTA; absolute dates; no markdown/bullets). Reuses shared `app/zai.py` — extended w/ optional `system`/`model`/`timeout` (backward-compatible); gem extracted from module-local copy (frontmatter stripped). `publicity_llm` config collapsed to `{model: glm-5}` (zai.py owns endpoint + `ZAI_API_KEY`).
- [x] 7. **Image lookup** — `/events/images` (Jina scrape + Pexels/Pixabay). Verify thumbnails render. ✓ 2026-07-24: live POST on the ULTRAMAN page → 4 real event images (hero/gallery, ~1024px, largest ranked first); self-check passes (svg/logo skipped, larger crops rank first). Pexels/Pixabay stock fallback deferred — keys absent, and the event's own images are higher-signal anyway.
- [~] 8. **Events create + thick-box UI** — `/events/create` (tian desk, body=bundle) + the EVENTS thick-box mode. Verify full event→bundle→doc+card flow. ⧖ 2026-07-24: thick-box UI shipped (EVENTS scout→bundle→images live) + `/events/create` written (delegates to /provision with the tian desk). Full event→doc+card verifies once the Google token mints. **← full EVENTS flow usable after the token lands.**
- [ ] 9. **Polish** — full 5-state coverage on every card, shared JOBS feed wiring, preview card.

**Shippability guarantee:** each slice leaves the module in a working, committable state. The
WRITERS half is done at slice 4; the EVENTS half at slice 8; slice 9 is polish only. If the build
stops at any ticked slice, something real works — never a half-broken tree.

## Verification (end-to-end — run it, don't just read it; f5-vibe-check habit)

- **WRITERS:** Paul, count 3 → 3 blank Docs in `1w8ESe…` (title only) + 3 cards in "To draft
  (Paul)", each card has the Doc attached; names `[202607] [CAT] #01..#03`; re-run → #04+.
- **EVENTS:** SCOUT → pick an event → confirm URL(s) → GENERATE BUNDLE → edit if needed → CREATE
  DOC+CARD → Doc in the TIAN folder titled `[202607] [EN] "<event>"` whose body **is the publicity
  bundle**; card "Event | <event>" in "To draft (TIAN)" with desc = event URL(s) + image links; Doc attached.
- **Portability:** grep the new files — zero hardcoded ids/paths/creds outside `options:`.
- **Self-check:** `python3 -m app.thailandnow` runs `assert`-based checks on token-refresh,
  name-token resolution, and the `{nn}` dedup logic (the non-trivial bits).

## Safepoint & resume protocol (multi-session build)

This build will likely span sessions — post-`/compact`, a fresh session, or a cheaper implementer
(GLM via agent-x). **The plan + git history are the recovery source.** Neither depends on this
conversation's context, which `/compact` summarizes away.

**First build action (do this before slice 1):** copy this plan into the repo as
`Railjack/docs/thailandnow-plan.md` and track all progress there. Then it is (a) version-controlled
alongside the code, (b) discoverable from a session launched inside the Railjack repo, and (c)
carried to Somatic on port (Somatic's build reads the same plan). The `~/.claude/plans/` copy is
the seed only — the repo copy becomes canonical once building starts.

**After every slice:**
1. **Verify by running it** (f5-vibe-check: run the app, don't just read the diff — Naz is a
   non-coder and cannot catch mistakes by reading).
2. **Commit** on `machine/railjack` — `feat(thailandnow): slice N — <what>`. This is the durable
   checkpoint you can roll back to.
3. **Tick the slice** in the Progress tracker above; note the commit short-hash and any carry-over
   TODO inline (e.g. `- [x] 3. … (a1b2c3d; TIAN list id still TODO)`).
4. **Commit the plan/progress update** in the same commit or immediately after, so the tracker in
   git matches reality.

**To resume in a fresh / post-compact session:**
1. Read `Railjack/docs/thailandnow-plan.md` end-to-end — it is self-contained (architecture, files,
   endpoints, config, gem, decisions, this protocol).
2. Find the **first unchecked slice** in the Progress tracker — everything above it is done.
3. Sanity-check: `git -C "~/Coding Projects/Railjack" log --oneline | grep thailandnow` — committed
   slices should match the tracker.
4. Re-read that slice's line + the Backend/Frontend sections it touches, then continue.

**If context runs low mid-slice:** commit the partial work tagged `WIP slice N` in the message,
and set the tracker to `[~] N — IN PROGRESS, stuck at <X>` with the exact next step. **Never end a
session with uncommitted work** — that is the only thing a new session cannot recover.

**Session handoff (optional, for cross-sister visibility):** after slice 4 and slice 8, drop a
one-line status in `~/Cephalon/hot.md` (e.g. "thailandnow module: WRITERS half live (slice 4);
EVENTS next") so the office side knows the home build's state.

## Notes / minor decisions made (flag if you disagree)

- **LLM provider for the bundle** defaults to Railjack's existing home gateway (z.ai/OmniRoute,
  `glm-5`). The gem currently runs on Gemini at the office for quality; if you want byte-for-byte
  parity, add a Gemini key + retarget `publicity_llm.base_url/model`. One-line config swap.
- **Gem prompt** lives module-local (`app/gems/event-publicity.md`) so it ports with the code; keep
  it in sync with the vault canonical (`gemini-gem-thailandnow-event-publicity.md`).
- **Direct Trello REST** (not the office n8n webhook) so the module is self-contained and Somatic
  doesn't depend on Orokin's n8n.
- Board `VTMuHmEj` and the three list IDs aren't cached anywhere — resolved once via `/setup`.
