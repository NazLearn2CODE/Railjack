# RADIO ▸ News Fill — streaming IDE-SCOUT port to Railjack (build brief)

**Date:** 2026-11-11 · **Builder:** agy (acceptEdits) · **Host verifies:** H
**Provenance:** O ran this exact workflow end-to-end on Somatic 2026-11-11 —
21/21 pieces, zero failures (Naz: "went without a hitch"). Naz-directed handoff
(O → H, 2026-11-11, `readme-naz.md` § Sister handoffs). This is a NATIVE
Railjack port: read the reference, write Railjack-idiomatic code. **Never
byte-copy across repos** — the lineages differ (Somatic: `app/radio_convert.py`
at repo root, `~/.config/somatic/`; Railjack: radio wrapper family in
`app/media/`, config under `~/.config/railjack/`, panel helpers/style differ).

## Goal

Railjack's RADIO ▸ News Fill gains the proven two-button streaming workflow:

📋 **IDE SCOUT** (exists — upgrade its prompt) → **CONVERT** (import pool /
convert selected) → tick pieces in broadcast order → **CONFIRM & APPLY TO DOC**
(streaming loop: verbatim explicit-slot writes, figure-verify, server-side
FORMAT, fail-safe) → done.

## Read first (all local, exact commits)

Reference implementation (Somatic, **@`4611b1f`** — local clone
`~/Coding Projects/Somatic`, already on `4611b1f`):

- `app/radio_convert.py` — `split_title_body` (lint + ENFORCED title),
  `cache_get/cache_put` (durable per-URL cache), `convert_piece`,
  `start_convert`/`convert_status` (pollable job), `plan_placement`
  (doc-verified dedup: skip only when ledger AND doc text agree),
  `apply_converted` (hard-gate).
- `app/radio_loop.py` — ledger load/append, `_filter_written`,
  `_write_explicit` (per-attempt error capture), `_run_loop` (ONE streaming
  assembly line: plan once → per piece rewritten?-skip-else cache-or-convert
  under 300 s hard timeout → verbatim explicit-slot write → ledger →
  server-side FORMAT at loop end), `start_loop`/`loop_status`.
- `app/newsroom.py` — how convert/loop/preview/format endpoints wrap the
  modules (search `radio/convert`, `radio/loop`, `radio/preview`).
- `frontend/src/components/NewsroomPanel.tsx` — CONVERT dual-mode button,
  tick-freeze order, ⚠ TITLE / ⚠ VERIFY badges, placement preview table,
  CONFIRM & APPLY streaming log, FAILSAFE rows.
- `tests/test_radio_convert.py`, `tests/test_radio_loop.py` — test SHAPES to
  port natively (mock the CLI subprocess; never hit Drive in tests).
- Commit history for rationale: `56704a1` (two-button split), `39ba77a`
  (one streaming pipeline), `000818f` (enforced title + server FORMAT),
  `3ab4cfc` (IDE SCOUT revert: `rewritten: true` contract), `d9fe555`
  (timeout armor + parallel FORMAT tagging), `4611b1f` (doc-verified dedup),
  `e13782f` (percent normalization — for the humanizer fold-in).

Railjack (the repo you are editing):

- `app/media/radio_news.py` — the thin subprocess wrapper (SCRIPTS =
  `~/.claude/skills/newsroom/scripts`; argv lists via
  `asyncio.create_subprocess_exec`; `_fatal` → 400 vs stderr → 502). Extend
  it; do not replace its contract style.
- `app/newsroom/newsroom.py` — existing radio endpoints (`/api/newsroom/radio/…`)
  and where new routes belong; `app/newsroom/humanizer.py` (113 lines).
- `frontend/src/components/NewsroomPanel.tsx` — radio section (IDE SCOUT
  buttons ~line 3197+), reuse `fetchJSON`/`post`/`error`, phosphor/HUD styling.
- `tests/test_radio_scout_state.py`, `tests/test_radio_slotmap.py`,
  `tests/test_newsroom_humanizer.py` — existing conventions to match.
- `RADIO-NEWS-BRIEF.md` — the fill-map contract (tabs/slots).

Vault contracts (canonical, read-only):

- `~/Cephalon/10-knowledge/radio-news-antigravity-handoff.md` — the handoff
  pool + rewrite contracts the IDE child follows.
- `~/Cephalon/10-knowledge/newsroom-rewrite-antigravity-handoff.md` — the
  second contract referenced by the scout prompt.
- `~/.claude/skills/humanizer/SKILL.md`, `~/.claude/skills/unslop/SKILL.md` —
  referenced BY PATH in the IDE SCOUT prompt (they exist on this machine).

Skill-library CLI (already current at `161d8d7` — synced; do NOT edit it):
`~/.claude/skills/newsroom/scripts/radio_news.py` subcommands:
`list-docs · browse · report · fill (--doc --kind --category --positional
--explicit) · slots (--doc --kind --category) · text (--doc) · autofill`.
`fill --explicit` consumes the explicit slot map (read the script for the
exact stdin shape); `text` prints per-tab raw text (doc-verified dedup).
`doc_format.py` already carries the realpath sys.path guard + parallel block
tagging server-side.

## Architecture (pinned — do not re-litigate)

1. **New modules** `app/media/radio_convert.py` and `app/media/radio_loop.py`
   (Railjack's radio wrapper family lives in `app/media/`; keeps
   `newsroom.py` from growing). Routers included in `app/main.py` next to
   `radio_news_router`.
2. **Durable cache** `~/.config/railjack/radio_converted/` (keyed
   url+content-sha; content change → stale → re-convert; never /tmp).
3. **Endpoints** (mirror Somatic's shapes, Railjack prefix
   `/api/newsroom/radio/...`): convert (dual-mode: pool import / selected),
   convert status poll, placement preview (sends titles; preview == write
   contract), apply/start + status poll (the streaming loop). Keep existing
   endpoints working — this is additive.
4. **IDE SCOUT pieces ship `rewritten: true`** → loop SKIPS cache/convert and
   writes title/body verbatim into the explicit slot. Figure-verify STAYS
   (120 s advisory timeout — failures never fatal). Raw (non-rewritten)
   pieces take the full convert pipeline defensively.
5. **Fail-safe (Naz verbatim ruling):** convert timeout/death or refused slot
   ⇒ `[CONVERT FAILED — <url>]` + reason written INTO the slot; piece NOT
   ledgered → retryable on re-run. Fill subprocess failure is caught PER
   ATTEMPT (2 tries) — one slow fill must never kill the loop task.
6. **Enforced title:** lint (leaked `Headline:`/`EN:`/`TH:` prefixes;
   >16-word "titles" = intro-takeover) then ENFORCE — first line over the
   house word cap demotes to first body paragraph, pool piece's source
   headline becomes the title, trailing period stripped.
7. **Doc-verified dedup:** the ledger is a hint, the doc is the truth.
   `plan_placement` reads doc text (CLI `text` subcommand): skip as "already"
   ONLY when ledger AND doc agree (headline present in text). No pre-skip on
   the ledger alone.
8. **Server-side FORMAT at loop end** (`job["format"]` in the poll result),
   idempotent, failure-isolated, wrapper timeout 420 s. Parallel block
   tagging comes free from the current `doc_format.py`.
9. **Entry points funnel through ONE loop** — new panel buttons, any stale
   bundle, and AUTOPILOT all hit the same streaming path. Keep the existing
   job/poller contract shape.
10. **Humanizer fold-in:** add the deterministic percent-normalization pass
    from Somatic `e13782f` to `app/newsroom/humanizer.py` (house style:
    "%"-figures → "percent"; `\b` guard leaves "percentage" untouched).
    Port the exact rule from the Somatic diff — do not invent a variant.
11. **IDE SCOUT prompt upgrade** (mirror Somatic `3ab4cfc`): the copied prompt
    must have the child read the two vault contracts (absolute paths above)
    PLUS `~/.claude/skills/humanizer/SKILL.md` and
    `~/.claude/skills/unslop/SKILL.md`, rewrite every piece into the radio
    shape (fresh headline ≤12 words as `title`, no `HEADLINE:` prefix, never
    the lede; body ≥180 ≤250 words), run humanize + unslop on its own draft
    (facts/names/numbers exact; "percent", never "per cent"), and set
    `rewritten: true` on every piece it returns.

## Work plan (file by file)

1. `app/media/radio_news.py` — add argv builders: `slots`, `text`,
   `fill --explicit` (existing `fill` untouched).
2. `app/media/radio_convert.py` (new) — cache, `split_title_body` (lint +
   enforce), `convert_piece` (4-pass rewrite → humanize via existing
   `app/newsroom/humanizer.py` → percent → figure-verify → explicit split),
   pollable convert job, `plan_placement` (doc-verified dedup via `text`),
   hard-gate apply helper.
3. `app/media/radio_loop.py` (new) — ledger, `_filter_written` (no pre-skip),
   `_write_explicit` (per-attempt armor), `_run_loop` (streaming; fail-safe;
   server-side FORMAT at loop end), `start_loop`/`loop_status`.
4. Endpoints wiring + router include in `app/main.py`.
5. `app/newsroom/humanizer.py` — percent pass (pin 10).
6. `frontend/src/components/NewsroomPanel.tsx` — CONVERT dual-mode, tick
   freeze + preview table, ⚠ badges, CONFIRM & APPLY streaming log, FAILSAFE
   rows, upgraded IDE SCOUT prompt (pin 11).
7. Tests (pytest, subprocess mocked — same shapes as Somatic's):
   `tests/test_radio_convert.py` (lint, enforce, cache staleness, dedup
   ledger-vs-doc matrix), `tests/test_radio_loop.py` (rewritten skip,
   fail-safe URL-in-slot, per-attempt armor, server FORMAT flag, ledger
   append/no-append), `tests/test_newsroom_humanizer.py` (percent pass).

## Out of scope (do NOT touch)

- skill-library scripts (already current; `queue.py` rename is coordinated
  with O — the realpath guard already protects Railjack's subprocess calls).
- REPLACE mode (floated, Naz hasn't asked). AUTOPILOT redesign (it just rides
  the loop). Live Drive writes (host does live verify with Naz's docs).
- Naz's `ThailandNowPanel.tsx` history — if you see unrelated uncommitted
  edits, leave them exactly as they are.

## Gates (run them yourself; report REAL output, never "should pass")

1. `python3 -m pytest tests/ -q` — full suite green (existing ~130+ tests must
   stay green; new tests above).
2. `cd frontend && pnpm tsc --noEmit && pnpm build` (or the repo's script —
   check `frontend/package.json`) — frontend builds clean.
3. Commit on `main` with a message naming the port; do NOT push (host pushes
   after live verify).

Report: files changed, test output tail, build output tail, any deviation
from this brief with a one-line reason.
