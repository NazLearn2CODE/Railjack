# NEWSROOM ▸ Queue REWRITE — SLICE A (backend + vault + tests)

First of two slices. Full context + gem paths + reuse callouts live in
`NEWSROOM-REWRITE-BEN-IDE-BRIEF.md` — read it first. This slice **freezes the API contract**
that Slice B (frontend) builds against. Follow `AGENTS.md` (implement-don't-copy,
verify-by-running, free-first/OmniRoute). Spawned from `~/Cephalon`.

## Scope (this slice ONLY)
- `app/newsroom.py::api_rewrite` (`POST /api/newsroom/rewrite`)
- `~/Cephalon/10-knowledge/skills/newsroom/scripts/nl_append.py`
- `tests/test_newsroom.py`

**Do NOT touch the frontend** (`NewsroomPanel.tsx`) — that's Slice B.

## Build

### 1. `app/newsroom.py::api_rewrite`
- Voice source → Ben: `BEN_GEM = Path(__file__).parent / "gems" / "radio-news-rewrite.md"`;
  `_gem_text()` loads the gem **body only** (strip the YAML frontmatter and the
  `## Notes (ignored…)` trailer — keep `## Role & Purpose` → `### Output`). Drop the
  `~/Gems` / `gemini-gem-news-rules.md` sources for THIS endpoint.
- Prompt = Ben body + **output override** + **Thai-name override**.
  - **Thai-name override**: keep the existing CRITICAL EDITORIAL RULE block **VERBATIM**
    (source-only — no facts from training; and never translate/transliterate any person's
    NAME or TITLE/rank — leave names + honorifics in the ORIGINAL THAI SCRIPT; translate
    the rest to English).
  - **Output override** (replaces Ben's "strict JSON" instruction): output the broadcast
    rewrite as **readable prose** (Ben's hard rules + voice still apply), and **wrap every
    person's NAME in `**double-stars**`** — the ONE exception to Ben's "no markdown" rule #6
    (the markers become bold in the Doc). No JSON, no preamble.
- **Second `zai.zai_message` call** for SEO: `system` = body of
  `~/Cephalon/10-knowledge/ai-workflow/gemini-gem-thailandnow-seo.md` + override ("produce
  ONLY the AI SEO Block — Version A (40-60w summary) + Version B (key points); skip focus
  keyphrases / meta descriptions / hashtags"); `user` = the source article text. Keep the
  SEO gem's house style (complete sentences, THB-first, absolute dates, repeat full entity
  names — never start a sentence with a pronoun).
- Return `{"rewritten": <Ben prose, **name** markers intact>, "seo": <A+B block>}`.
- **Exactly two** gateway calls (Ben + SEO). Empty result on either → existing 400/502 behavior.

### 2. `nl_append.py` (VAULT file)
- Keep `**name**`→bold as-is (`BOLD`/`parse_bold`/insert-bold loop already exist). **ADD**
  date/relative-time **underline** in the SAME batchUpdate: reuse `doc_format.py`'s
  `DATE_RE`/`find_dates` (import if clean, else copy the one regex with a
  `# sourced from doc_format.py` cite). Find date spans in `plain` (the marker-stripped text,
  AFTER `parse_bold`), and emit `updateTextStyle {underline:true, fields:"underline"}` per
  span — mirror the existing bold-span loop. **Backward-compatible**: no `**` markers and no
  dates ⇒ behaves exactly as today.
- Vault hygiene: atomic write (temp + `mv`), `git pull` before / `git push` after, preserve
  frontmatter, append a line to `~/Cephalon/logs/memory-log.md`.

### 3. `tests/test_newsroom.py`
- Rewrite prompt loads **Ben's** gem (not news-producer), still contains the Thai-name rule,
  and instructs `**name**` markers.
- SEO fires as a **separate** call with the A+B override.
- Response shape `{rewritten, seo}`.
- Unit-test `nl_append` on a sample string with `**Name**` + a date → bold spans AND underline
  spans emitted (mock the Docs API; assert the batchUpdate requests).

## Frozen contract for Slice B
`POST /api/newsroom/rewrite` body `{text: str}` → `200 {"rewritten": str, "seo": str}`.
- `rewritten` = broadcast prose with **literal `**name**` markers** around person names.
- `seo` = AI SEO Block (Version A + B) text.

## Landing bar
- `cd ~/Coding\ Projects/Railjack && python3 -m pytest tests/test_newsroom.py -q` → green.
- Live: `POST /api/newsroom/rewrite` with a PRD article → Ben prose with `**name**` markers,
  Thai names/titles in original Thai script, + A+B SEO block.
- SEND TO NL → today's NL Doc shows person names **bold** and dates/relative-times
  **underlined** (beneath the `***END CREDIT***` table).
- Token check: **two** gateway calls per rewrite, no third tagger call.

## Do not
- No frontend edits (Slice B). No RADIO sub-tab. No separate LLM name-tagger (markers + DATE_RE
  replace it). No SEO appended to the Doc. No edits to Ben's gem or the SEO gem — load read-only.
