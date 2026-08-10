# Fireside Mode — Backend Build Brief (Slice 1)

> **For:** agy (builder). **Verifier:** Tawhan (host). **Status:** ready to build.
> Source design: `~/.claude/plans/so-let-s-be-even-rustling-hennessy.md`.
> Frontend is **Slice 2** (separate brief); live gate is **post-notebook** (T7).

## Context
NBT World's **The Fireside** is a weekly two-host YouTube show (anchor host **Ben
Rujopakarn** + a rotating co-host; foreigner-in-Thailand audience; ~77 episodes across two
runs). Ben is outsourcing **topic sourcing + edit notes** to Naz. This build adds a third
Story Scout mode — **FIRESIDE** — with two sub-flows: **SOURCE TOPICS** (suggest fresh,
not-already-done episode topics, grounded in a NotebookLM corpus of all episodes) and
**EDIT NOTES** (editorial notes on a draft script in Ben's voice).

## What to build (backend only — `app/thailandnow.py` + 2 gems)

All additions are **additive** (new helpers/routes; do not modify existing routes). No changes
to `app/main.py` (routes mount via `thailandnow_router`).

### A. NotebookLM corpus discovery (model on the `_TN_NB_PREFIX` pattern at L1713)
```python
from .notebooklm import _run_cli, _cached_notebooks   # import; do NOT write a 3rd CLI runner

_FIRESIDE_NB_PREFIX = "The Fireside"

def _fireside_nb_id_path() -> Path:
    # sidecar: ~/.config/railjack/fireside_notebook.id  (model on _nlm_notebook_id_path L1641)

def _fireside_nid() -> str | None:
    # 3-layer lookup, first hit wins:
    #   1. options:  opts.get("fireside_notebook_id")
    #   2. sidecar:  read _fireside_nb_id_path() if present
    #   3. discover: scan _cached_notebooks() for title startswith _FIRESIDE_NB_PREFIX
    #   else None

def _fireside_ensure() -> str:
    # nid = _fireside_nid(); if None -> raise HTTPException(424,
    #   "Create a notebook titled 'The Fireside…' in the RESEARCH tab, add the episode sources,
    #    wait for READY, then retry — or set fireside_notebook_id in options.")
    # DISCOVER ONLY — never create. (Do not call the dead _nlm_ensure_notebook L1668.)
```

### B. Topic Registry reader (Google Sheet — status-of-truth for done/revisit/exclude)
Sheet ID `1JG7xFiCmMgPx4APFB2U9tRj56yVP5Abz36t0bi0BgWs` (overridable via `options:
fireside_registry_sheet_id`). Tab `Topics`. Columns:
`VideoID | Run | EP | Topic | Status | Co-host | UploadDate | Angle/Notes`.
`Status` ∈ {`done`, `revisitable`, `excluded`}.
```python
def _fireside_registry() -> list[dict]:
    # Read the sheet via the SAME Google-credentials helper the rest of thailandnow.py uses
    # for Drive/Sheets (locate it in this file or app/ — reuse, don't add a new auth path).
    # Return [{video_id, run, ep, topic, status, ...}]. Cache per-request is fine.
```

### C. SOURCE TOPICS flow + route (async, job-store, single-flight)
```python
def _flow_fireside_source(job, seed: str, category: str | None):
    # 1. registry = _fireside_registry(); drop status in {"done","excluded"};
    #    keep "revisitable" tagged as update-candidates.
    # 2. nid = _fireside_ensure()
    # 3. ask the corpus notebook (templated prompt): "Suggest a fresh episode topic on {seed}
    #    NOT in this done-list [{done topics}]. Adjacent episode #s. Frame as the two questions
    #    a foreigner-in-Thailand asks. 2-4 citable source URLs. An 'If You Like A, Try B' pairing
    #    with a past episode. Visual/chapter-card style used for similar episodes."
    #    -> notebooklm ask (synchronous, via _run_cli / the notebooklm.py ask path). Map
    #    references[].source_id -> URLs via the source list.
    # 4. ASK-THIN GUARD: if answer empty / refs empty -> RELAXED web fallback:
    #    a _scout_news-style sweep (EN+TH+site:) but NO date filter — model on
    #    _scout_lookup_title L1054 ("keep undated"), NOT _scout_news/_scout_date_in_range L1006.
    #    tag mode="web-fallback".
    # 5. Shape pass: _load_gem(_fireside_source_gem_path()) + zai_message(notebook_answer+
    #    mapped_urls+seed, system=gem) -> strict JSON via _parse_json_lenient (L708):
    #    {topics:[{title, angle, ep_adjacent:[..], source_urls:[..], if_like_a_try_b,
    #    visual_style, why_fresh, revisit_candidate:bool}]}
    # 6. job.result = {"topics":..., "mode":"notebook"|"web-fallback", "notebook_id":nid}

# Routes (model scout_search L1471 + scout_report L1486):
POST /api/thailandnow/scout/fireside/source      body {seed?, category?}  -> single-flight kind "fireside-source" -> _tn_spawn -> {id}
GET  /api/thailandnow/scout/fireside/source/report/{jid}                  -> {topics, mode, notebook_id}
```

### D. EDIT NOTES flow + route (synchronous — model `scout_pitch` L1497)
```python
async def _fireside_edit(draft: str | None, url: str | None, check_coverage: bool = False):
    # text = draft if draft else _jina_read(url) (public Google Docs only; if thin/empty ->
    #   return {mode:"degraded", error:"paste the draft — couldn't read the URL"})
    # gem = _load_gem(_fireside_edit_gem_path())   # _resolve_gem L1080 pattern
    # notes = _parse_json_lenient(await zai_message(user=text, system=gem, timeout=180))
    # if check_coverage: second notebook ask "has this angle been covered? which EPs?" ->
    #   notes["coverage_check"] (empty string on thin, never an error)
    # return {"notes": notes, "mode": "direct"|"degraded"}

POST /api/thailandnow/scout/fireside/edit-notes   body {draft?, url?, check_coverage?}  -> {notes, mode}
```

### E. Gems (`app/gems/`)
- **`fireside-source.md`** — thin shaper; enforces the strict-JSON 5-field contract above +
  the "two questions" angle framing + If-Like-A-Try-B + visual-style. Discipline mirrors
  `app/gems/story-scout-pitch.md`.
- **`fireside-edit-notes.md`** — two-host Fireside editorial-notes voice. Returns JSON
  `{overall, strengths[], fixes[{anchor,note,severity}], structure_notes, voice_notes,
  coverage_check}`. **Ben-anchored + co-host-AGNOSTIC** (never hardcode a co-host name; handle
  whatever name appears). Voice v1 below — **refine after the corpus voice-distillation**:
  > Ben = anchor host + editor. Two-host chat. "Two questions" framing. Ranking/list structure,
  > "If You Like A, Try B" pairings, audience "Discussion:" prompts, clear signposting, confident
  > + clean, no hype the source doesn't support. Edit notes = specific, prioritized
  > (must/should/nit), anchored to quoted phrases.

### F. Config (`options:` — portability gate)
Optional overrides: `fireside_notebook_id`, `fireside_registry_sheet_id`,
`fireside_source_gem_path` (default `app/gems/fireside-source.md`),
`fireside_edit_gem_path` (default `app/gems/fireside-edit-notes.md`). Resolve via the existing
`_resolve_gem` (L1080) / options pattern. **Nothing hardcodes an id/path/cred inline.**

## Hard constraints (portability — `docs/thailandnow-plan.md` § Portability, `TOPOLOGY.md`)
- Zero hardcoded notebook id / sheet id / path / cred outside `options:`. Somatic adopts by
  reimplementing from this reference (never `git merge`).
- NotebookLM CLI: every call passes `-n <nid>` explicitly (never `notebooklm use` — parallel-unsafe).
- Do NOT touch `newsroom/rewrite`, `radio-news-rewrite.md`, the SEO gem, or name-overlay code —
  wrong shape for two-host edit notes. Do NOT delete the dead `_nlm_ensure_notebook` (L1668).
- Single-flight: use kind `"fireside-source"` (distinct from `"scout-search"` — don't block PITCH).

## Verification gate (agy runs; host re-checks)
```
.venv/bin/python -c "import app.thailandnow; from app.thailandnow import _fireside_nid, _fireside_registry, _fireside_ensure; print('import-ok')"
.venv/bin/python -m pytest tests/test_scout.py -q          # existing scout tests still green
.venv/bin/ruff check app/thailandnow.py app/gems 2>/dev/null || true
```
Plus add focused tests to `tests/test_scout.py` (plain pytest, no framework): registry-status
filter (done/excluded dropped, revisitable kept), the ask-thin→web-fallback mode tag, and
`_parse_json_lenient` on a sample edit-notes payload. Live SOURCE/EDIT verification (needs the
corpus notebook) is **T7, after auth + notebook build** — not this slice.

## Out of scope for this slice
- Frontend (FIRESIDE MODE toggle + SOURCE/EDIT sub-views) = **Slice 2**.
- Building/populating the corpus notebook + voice distillation (Phase A, blocked on notebooklm
  auth — host handles).
- Somatic port (post home-green).
