# NEWSROOM ▸ Queue REWRITE — SLICE C (revision: name overlay + date markers)

Revises the SHIPPED Slice A+B (Railjack `a7a0bf8`, vault `eec9b05`) per editor feedback.
Full context in `NEWSROOM-REWRITE-BEN-IDE-BRIEF.md` (v2 clauses supersede v1). Spawned from
`~/Cephalon`; follow `AGENTS.md`.

## What changes (v2)
1. **Name overlay `[Official English(Thai)]`** — replaces the v1 "keep Thai only" rule.
   For each person the SOURCE names, Ben recalls their **official English name** (the
   established public rendering — e.g. a minister's known English spelling) and outputs
   `**[English(Thai)]**`. If Ben cannot confidently confirm an official English name →
   fall back to `**Thai name**` (bold-marked, NO transliteration, NO guessing). Editors
   fix gaps. **Titles/ranks stay in original Thai.**
   - **Narrow source-only carve-out:** Ben may use knowledge ONLY for a named person's
     official English name-form — never to ADD names, never for dates/figures/events/facts.
2. **`-/date/-` underline markers** — Ben wraps every date, time, and relative-time in
   `-/…/-` (e.g. `-/July 15, 2026/-`, `-/3:00 PM/-`, `-/next month/-`). These become
   underlined in the Doc, mirroring `**name**`→bold.
3. SEO unchanged (already A+B).

## Build

### A. `app/newsroom.py::api_rewrite` — the prompt
- **Replace the name clause** in the CRITICAL EDITORIAL RULE: from "leave names in original
  Thai script" → the overlay rule above. Spell out: official English name → `**[English(Thai)]**`;
  unknown → `**Thai name**` fallback; no transliteration; titles stay Thai. Add the explicit
  carve-out sentence (knowledge allowed ONLY for a named person's official English name-form;
  all else source-only).
- **Add to the OUTPUT OVERRIDE:** "Wrap every date, time, and relative-time expression in
  `-/…/-` markers (e.g. `-/July 15, 2026/-`, `-/next month/-`). These become underlined."
- Keep `**name**` bold markers (around the overlay token or the Thai fallback). SEO call unchanged.

### B. `~/Cephalon/…/newsroom/scripts/nl_append.py` (VAULT file)
- Add an UNDERLINE marker parser mirroring `BOLD`: `UNDERLINE = re.compile(r"-/(.+?)/-")`;
  strip markers → underline spans on `plain` (same coordinate system as bold); emit
  `updateTextStyle {underline:true}` per span.
- **Keep** the existing `DATE_RE` regex pass as an idempotent backstop (catches any date Ben
  missed; re-setting underline=true is a no-op). So both markers AND regex feed underline safely.
- Vault hygiene: atomic write (temp + `mv`), `git pull`/`push`, preserve frontmatter, memory-log line.

### C. `frontend/src/components/NewsroomPanel.tsx::renderRewritePreview`
- Extend the parser to also render `-/…/-` → `<u>escaped</u>` (alongside `**…**` → `<strong>`).
  Escape everything else (reuse `escapeHtml`). The `[English(Thai)]` overlay is plain text inside
  the `**` markers — no special handling needed.

### D. Tests — `tests/test_newsroom.py`
- Rewrite-prompt test: name clause now asserts the overlay rule + the `-/date/-` instruction +
  the source-only carve-out.
- nl_append: sample with `**Name**` + `-/date/-` → bold span AND underline span emitted.
- Adjust any existing assertion that encoded v1 (Thai-only / regex-only dates).

## Landing bar
- `uv run pytest tests/test_newsroom.py -q` green; `npm run build` (tsc + vite) clean.
- Live on :8700: names render as `[English(Thai)]` (or Thai-only fallback), bold; dates/times/
  relative-times wrapped in `-/…/-` in `rewritten`; SEND TO NL → bold names + underlined dates.

## Do not
- NO transliteration — official English names only, else Thai-only fallback.
- Don't change SEO. Don't touch RADIO. No position-targeted insertion / Radio button (phase 2).
- Don't edit Ben's gem or the SEO gem — override in code.
