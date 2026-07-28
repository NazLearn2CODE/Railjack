# NEWSROOM ▸ Queue REWRITE — Antigravity IDE brief (Ben voice + AI SEO + marker formatting)

> **Built in 2 slices** — A: `NEWSROOM-REWRITE-SLICE-A-BACKEND.md` (backend + vault + tests;
> freezes the API contract) → B: `NEWSROOM-REWRITE-SLICE-B-FRONTEND.md` (frontend, after A
> lands). **+ SLICE C (revision, 2026-07-28): `NEWSROOM-REWRITE-SLICE-C-REVISION.md`** — name
> overlay `[Official English(Thai)]` + `-/date/-` underline markers. v2 clauses below supersede
> the v1 name/date rules where they conflict. Read this file for rationale, gem paths, reuse.

Re-voice the newsroom **queue** sub-tab's REWRITE. Today `POST /api/newsroom/rewrite`
runs the old news-producer Rules Gem and emits a two-layer TV script. Re-point it to
**Ben, editor of Thailand NOW** (the voice already in `app/gems/radio-news-rewrite.md`),
**keep the Thai-name rule**, add an **AI SEO block (Version A+B)**, and make the output's
`**name**` markers + dates become real **bold / underline** when SEND TO NL drops the
script into the Google Doc. **Zero extra LLM calls** — formatting is deterministic
(markers + regex), which is the whole point.

Phase 1 only. Follow `AGENTS.md` (implement-don't-copy, verify-by-running, free-first /
OmniRoute). Spawned from `~/Cephalon`; edit under `~/Coding Projects/Railjack` + the one
vault script noted.

## Read first
- `app/newsroom.py` — `api_rewrite` (`POST /api/newsroom/rewrite`, ~L184), `_gem_text()`
  (~L177), `GEM`/`GEM_FALLBACK` constants (~L34). Imports `from . import zai` (the OmniRoute
  gateway). The current CRITICAL EDITORIAL RULE block (~L197-211) = the Thai-name rule to KEEP.
- `app/gems/radio-news-rewrite.md` — **Ben's voice gem** (new voice source). It is RADIO-tuned:
  its Output section demands strict JSON — you will OVERRIDE that. Load read-only; do not edit.
- `~/Cephalon/10-knowledge/ai-workflow/gemini-gem-thailandnow-seo.md` — SEO gem. Produces
  keyphrases / meta descriptions / hashtags / AI-block; you OVERRIDE to emit ONLY AI Block A+B.
- `frontend/src/components/NewsroomPanel.tsx` — `rewriteDoc` (~L20), `rewrite()` (~L562),
  `sendToNL()` (~L545), queue REWRITE button (~L716), preview iframe (~L735), "load into
  Script" (~L742). Reuse existing `fetchJSON`/`post`/`error` helpers + phosphor/HUD styling.
- `~/Cephalon/10-knowledge/skills/newsroom/scripts/nl_append.py` — the SEND TO NL script.
  **Already** parses `**name**`→bold: `BOLD` regex (L125), `parse_bold()` (L128), insert +
  bold spans (L144-165). You ADD date-underlining beside it.
- `~/Cephalon/10-knowledge/skills/newsroom/scripts/doc_format.py` — has `DATE_RE` (L166),
  `find_dates()` (L175), `_underline()` (L319) to REUSE (dates + relative times like
  yesterday / next month / this week).

## What to build

### A. Backend — `app/newsroom.py::api_rewrite`
1. Repoint the voice source to Ben: `BEN_GEM = Path(__file__).parent / "gems" / "radio-news-rewrite.md"`;
   `_gem_text()` loads it. Use the gem **body only** — strip the YAML frontmatter and the
   `## Notes (ignored…)` trailer (keep `## Role & Purpose` → `### Output`). Drop the
   `~/Gems`/`gemini-gem-news-rules.md` sources for THIS endpoint.
2. Rewrite prompt = Ben gem body + **output override** + **Thai-name override**.
   - **Thai-name override**: keep the existing CRITICAL EDITORIAL RULE block VERBATIM
     (source-only — no facts from training; and never translate/transliterate any person's
     NAME or TITLE/rank — leave names + honorifics in the ORIGINAL THAI SCRIPT exactly as
     the source writes them; translate the rest to English).
   - **Output override** (replaces Ben's "strict JSON" instruction): output the broadcast
     rewrite as **readable prose** — Ben's hard rules + voice still apply — and **wrap every
     person's NAME in `**double-stars**`**. This is the ONE exception to Ben's "no markdown"
     rule #6 (the markers become bold in the Doc). No JSON, no preamble, no commentary.
3. Add a **second** `zai.zai_message` call for SEO: `system` = SEO gem body + override
   ("produce ONLY the AI SEO Block — Version A (40-60w summary) + Version B (key points);
   skip focus keyphrases / meta descriptions / hashtags"); `user` = the source article text.
   Keep the SEO gem's house style (complete sentences, THB-first, absolute dates, repeat
   full entity names — never start a sentence with a pronoun).
4. Return `{"rewritten": <Ben prose, **name** markers intact>, "seo": <A+B block>}`.
   (Extends today's `{"rewritten": ...}`.)
5. Both calls ride `zai.zai_message` (OmniRoute). **Exactly two** gateway calls per rewrite.
   On either call returning empty → existing HTTPException behavior (400 empty / 502 empty).

### B. Vault script — `~/Cephalon/…/newsroom/scripts/nl_append.py`
- Keep `**name**`→bold as-is. **Add** date/relative-time **underline** in the SAME batchUpdate:
  reuse `doc_format.py`'s `DATE_RE`/`find_dates` (import if clean, else copy the one regex
  with a `# sourced from doc_format.py` cite). Find date spans in `plain` (the marker-stripped
  text, after `parse_bold`), and emit `updateTextStyle {underline:true, fields:"underline"}`
  per span — mirror the existing bold-span loop. **Backward-compatible**: no `**` markers and
  no dates ⇒ behaves exactly as today.
- This is a **vault file** → atomic write (temp + `mv`), `git pull` before / `git push` after,
  preserve frontmatter, append a line to `logs/memory-log.md`.

### C. Frontend — `frontend/src/components/NewsroomPanel.tsx`
1. `rewrite()`: read `{rewritten, seo}`; store both (`setRewritten(d.rewritten)`,
   `setSeo(d.seo || "")`).
2. Preview iframe (`rewriteDoc`): render `**name**` → `<strong>name</strong>` (HTML-escape
   everything else). Markers stay in the raw `rewritten` string so SEND TO NL can convert them.
   Below the prose, render the `seo` block (Version A + B) as a **copyable** region.
3. "load into Script": loads `rewritten` **with markers intact** into the Script textarea.
4. `sendToNL()`: **unchanged wire** — still posts `{today:true, text:sendText}` to
   `/api/newsroom/append`. The markers + dates become Doc formatting via the nl_append change.
   **Do NOT append the SEO block** to the NL Doc — it is panel-only / copyable.

### D. Tests — `tests/test_newsroom.py`
- Rewrite prompt loads **Ben's** gem (not news-producer), still contains the Thai-name rule,
  and instructs `**name**` markers.
- SEO fires as a **separate** call with the A+B override.
- Response shape `{rewritten, seo}`.
- Unit-test nl_append on a sample string containing `**Name**` + a date → bold spans AND
  underline spans emitted (mock the Docs API; assert the batchUpdate requests).

## Landing bar
- `cd ~/Coding\ Projects/Railjack && python3 -m pytest tests/test_newsroom.py -q` → green.
- `npm run build` (tsc + vite) → clean.
- On `:8700` → NEWSROOM → **queue** → REWRITE: preview shows **bold** person names + an AI
  SEO (A+B) block; "load into Script" keeps the markers; SEND TO NL writes the script to
  today's NL Doc with names **bold** and dates/relative-times **underlined** (beneath the
  `***END CREDIT***` table).
- Token check: **two** gateway calls per rewrite (Ben + SEO) — no third tagger call.

## Do not
- Do NOT touch the RADIO sub-tab or its format pass — queue-only.
- Do NOT run a separate LLM name-tagger for the queue — `**name**` markers + `DATE_RE` replace
  it (zero extra tokens; that is the design).
- Do NOT append the SEO block into the NL Doc.
- Do NOT build position-targeted insertion or a Radio-doc button (phase 2).
- Do NOT edit Ben's voice gem or the SEO gem — load read-only and override in code.
