# Fireside — Remaining Work (agy takeover brief)

> **For:** agy (builder, autonomous). **Verifier:** Tawhan (when her usage limit resets — she
> will check every item below live; do not consider any item "done" on self-report alone).
> **Context:** The Fireside mode is BUILT + verified live (STORY SCOUT ▸ FIRESIDE MODE ▸
> SOURCE TOPICS [notebook-grounded] + EDIT NOTES). `nlm` is installed (isolated, `uv tool`),
> authed, and the **78-source Fireside corpus** exists (notebook id
> `51dddb72-c2d8-4466-b75f-e66470f0b940`). The Fireside flow already calls `nlm` directly.
> Do NOT touch the Fireside flow or the Fireside gems' structure (only the edit-notes gem's
> *voice* section, item 1c).
> Work in `/var/home/NAZ/Coding Projects/Railjack` unless noted. Follow `agents-and-code-adw`
> (build → gate → host-verifies). **Naz commits repo changes himself — leave them staged/unstaged.**

## 🔴 BEN UPDATE — 2026-08-10 (URGENT — do Item 0 FIRST, then 1–3)
Ben needs a Fireside topic for the **Aug 23 episode**, pitches wanted **same-day** to test the writer. He shared his **Season 2 scripts** as a topic-DEVELOPMENT guide (NOT a writing guide) and stated his methodology. This makes pitching the live, recurring task; Items 1–3 below support it.

**Ben's topic-development methodology (encode into the SOURCE flow prompt + `app/gems/fireside-source.md`):** start from a concrete **news/event hook**, then broaden into 3–4 angles — **the cultural dimension, the industry/economic dimension, government policy, and an ASEAN/regional comparison.** His drone episode is the template: *drone association + commercial applications & industry growth + national-security challenges + ASEAN comparison.* A news hook is often just the excuse to "talk about Thailand more broadly."

**DEDUP FIX (proven necessary 2026-08-10):** a bare corpus query drifts to already-covered themes (it suggested re-treads of EP25/36/40/41). The SOURCE prompt MUST feed the **Registry's full done-list** (all 77 topics) as a hard AVOID. The flow currently caps at `done_topics[:60]` — **raise/remove the cap** and instruct "avoid these topics AND their themes; find genuinely new ground." (Tawhan's 2026-08-10 second batch — Kanchanaburi/ANZAC, Thai viticulture, traditional-medicine/sapan-wood — is the quality bar after the fix.)

## Item 0 — URGENT + recurring: Fireside pitching for Ben
1. **Ingest Ben's S2 scripts** as a topic-dev guide into the corpus (`51dddb72-c2d8-4466-b75f-e66470f0b940`). Drive folder `1ajj5Mb-tquw8NSstzN7UAukdRGfNRNNd` (Naz has access) holds **9 edition subfolders** (`ผลงานงวดที่ ๑`–`๙`) + loose docs **EP35: Landbridge** (`12XuVzhwDP6T4rJ2mJPnQS43U7ugJiWBnzpt13WkZX2E`) and **EP##: Episode Script Template** (`1ikCtMlUMYpMSDAgv-G-hX74Ca7ruWHHPxjG5mAe5qs8`). **EXCLUDE the `Queen` folder** (`13FFS28LXufYj522rm2Z7dUlGMFYo5oKn`) — Ben: "ignore the Queen ones, that'll fuck it up." Recurse each edition folder, add each script: `nlm source add <nid> --drive <doc-id> --type doc`. (Corpus 78 → ~110+ sources; fine on Plus/Pro.)
2. **Encode methodology + dedup fix** in `_flow_fireside_source`'s prompt + `fireside-source.md` (hook→4 angles; full done-list AVOID; ignore Queen/Mother's-Day topics).
3. **ADD a "find real sources + verify the hook" step to `_flow_fireside_source`** ⚠️ CRITICAL (verified 2026-08-10): the corpus sometimes FABRICATES specific hooks/dates — e.g. the "Aug-15 ANZAC memorial" was fabricated (ANZAC Day is April 25, not August) and the "Aug-18 sapan-wood grant" didn't confirm. The THEMES are all real + documented; only the specific dated EVENT hooks are suspect. **Web-verify each hook individually** — some DO check out on deeper search (the "Decanter-2026 golds" turned out real), so verify rather than auto-reject. So after the corpus proposes a topic, run a LIVE web pass (reuse `_brave_urls` / `_gnews_urls` / `_jina_read` / `_extract_news`) for the hook + each angle → return REAL article/report URLs and **replace the fabricated hook with the actual current event** (flag the topic if no real hook verifies). **Every topic card MUST ship 3–5 verified source URLs** (real articles/reports — NOT the corpus's YouTube-episode URLs) so Naz/Ben can dig in. (Naz requirement 2026-08-10.) This runs on BOTH the notebook path and the web-fallback path.
4. **Generate pitches on demand** when Naz kicks you off (Aug 23 + future episodes). **OUTPUT per card (the deliverable Naz/Ben receive):** punchy title · news/event hook (web-verified per step 3) · 3–4 development angles (culture/industry/policy/ASEAN) · why-timely-for-the-date · **an "if-you-liked-X" tie to the closest past episode** · verified real source URLs.
5. **EPISODE TIE — always populated when a relevant episode exists** (Naz requirement 2026-08-10): even on the web-first path, after a topic is pitched, **query the corpus** — `nlm notebook query <nid> "which past Fireside episode(s) are most topically adjacent to '<topic>'? give EP# + title"` — and fill `ep_adjacent` + `if_like_a_try_b` with the real matched episode(s). Say "new ground — no close past episode" only if the corpus finds none. Grounding the tie in the corpus (not the LLM's memory) is what makes it a *real* past-episode reference.
6. **Verify:** a SOURCING run returns topics NOT in the 77 done-list; the prompt includes methodology + done-list; **each card carries a verified real hook, real source URLs, AND a corpus-grounded episode tie** (or an honest "new ground" note).

## `nlm` cheat-sheet (already installed + authed — just call it)
```
nlm notebook list --json                         # → [{id,title,source_count,updated_at}]
nlm notebook create "Title" --json               # → {notebook_id,title,url}   (note: notebook_id, not id)
nlm source list <nid> --json                     # → [{id,title,type,url,status}]
nlm source add <nid> --youtube <u> [--youtube …] # repeatable; --drive <docid> --type doc; --wait
nlm notebook query <nid> "<question>" --json     # → {answer, citations:{num:source_id}, references, …}
```

---

## Item 1 — Phase A: visual/voice extraction + vault criteria note (task #4)
**Goal:** distill the Fireside format/topics/visuals/voice from the corpus into a durable vault note; refine the edit-notes gem voice.
1. **Visual extraction** — `nlm notebook query 51dddb72-c2d8-4466-b75f-e66470f0b940 "Distill The Fireside's recurring VISUAL/STORYBOARD style: chapter cards, lower-thirds, color palette, typography, segment dividers, on-screen text, the graphics-tab patterns. Be specific and concrete." --json` → save `answer`.
2. **Voice extraction** — `nlm notebook query 51dddb72-… "Distill Ben Rujopakarn's ANCHOR-HOST voice and the two-host Fireside format: segment types, Ben↔co-host interplay, signposting, the 'two questions' framing, list/ranking structure, 'If You Like A, Try B' pairings, audience Discussion prompts, tone. Ben is the constant; co-hosts rotate. Be concrete with examples." --json` → save `answer`.
3. **(c) Refine the edit-notes gem voice** — edit `app/gems/fireside-edit-notes.md`: replace the v1 placeholder voice with the distilled voice (item 2). Keep the JSON contract + co-host-agnostic rule. Bump the gem's `updated:` frontmatter.
4. **Write vault note** `~/Cephalon/10-knowledge/fireside-topic-criteria.md` (follow vault rules in `~/Cephalon/Cephalon.md`: YAML frontmatter `title/date/tags/category/status`, atomic, link don't duplicate). Sections: Fireside format + Ben's expectation (Naz's slice = **topic sourcing + edit notes**); the **77 done-topics** with run/EP (source: the Topic Registry Sheet `1JG7xFiCmMgPx4APFB2U9tRj56yVP5Abz36t0bi0BgWs` or `/tmp/fireside_registry.csv`); topic-pattern + selection criteria (evergreen deep-dive, foreigner-in-Thailand, ranking/comparative, "If You Like A Try B", not-already-done); visual/storyboard styles (item 1); voice (item 2); competitor gaps (note: none loaded yet — Naz to pick). Link `[[thailand-now-content-automation]]`, `[[railjack-somatic-topology]]`.
5. **Update** `~/Cephalon/10-knowledge/knowledge-index.md` (one-line pointer) + run `python3 ~/Cephalon/vault-check.py` (must be clean).

**Verify:** `rag_search "Fireside topic criteria"` returns the note; `vault-check.py` clean; the edit-notes gem loads (`_load_gem` non-empty).

---

## Item 2 — Phase C: storyboard prompts (task #7)
**Goal:** two paste-able prompts Naz drops into Google tools, grounded in the visual extraction (Item 1).
1. **Fireside Google Flow storyboard prompt** — for `labs.google/fx/tools/flow`. Renders one episode's beats as a scene-by-scene visual storyboard in the Fireside aesthetic (chapter cards, lower-thirds, palette, typography from Item 1). Parameterize the episode topic/angle so it's reusable.
2. **Google AI Studio storyboard-app builder prompt** — for AI Studio's app builder. A **generic** storyboard maker with **multiple baked-in art styles**, one specifically **"Fireside"** (Fireside etiquette/aesthetics from Item 1). Output = a shareable app link Naz sends Ben.
3. Save both to `~/Cephalon/10-knowledge/fireside-storyboard-prompts.md` (frontmatter + the two prompts in copy-paste blocks) + update `knowledge-index.md`.

**Verify:** both prompts are concrete, reference the Fireside visual style from Item 1, and are paste-ready (no placeholders Naz must fill beyond the episode topic).

---

## Item 3 — RESEARCH panel: `app/notebooklm.py` → `nlm` refactor (task #9 remainder)
**Goal:** the RESEARCH panel is broken (same dead-`notebooklm`-CLI root cause). Refactor `app/notebooklm.py` (the shared wrapper the RESEARCH panel uses) to call `nlm` instead of the old `notebooklm` CLI. The Fireside flow is NOT affected (it calls `nlm` directly already — don't touch it).
1. Map every old-CLI call in `app/notebooklm.py` to its `nlm` equivalent (cheat-sheet above). Watch the output-shape differences: `notebook list` is a **list** (not `{notebooks:…}`); `create` returns **`notebook_id`**; `source list` is a **list** of `{id,title,url,status}`; `query` returns `{answer, citations:{num:source_id}}` (citations is a **dict**).
2. Keep the public function signatures stable (the RESEARCH panel's `app/notebooklm.py` routes `/api/nblm/*` call them) — change only the internals (CLI runner + parsing). Set the CLI to `nlm`; adapt `_run_cli` command construction per call.
3. **Portability:** no hardcoded ids/paths/creds outside `options:`. Leave the old `notebooklm` CLI installed (just stop calling it). `nlm` auth is already valid on home.
4. **Gate (must pass):** `.venv/bin/python -m pytest -q` (full suite green) + `.venv/bin/python -c "import app.notebooklm"` + restart `systemctl --user restart railjack.service` + live: the RESEARCH panel lists notebooks / creates one / queries one via `nlm` (the things that 404/failed before).

**Verify (host will re-check):** RESEARCH panel live at `:8700` works end-to-end via `nlm`; full suite green; nothing else regressed.

---

## Done criteria (for Tawhan's post-reset check)
- [ ] Item 1: vault note exists + RAG-findable + vault-check clean; edit-notes gem voice refined.
- [ ] Item 2: two storyboard prompts in the vault, paste-ready.
- [ ] Item 3: RESEARCH panel works live via `nlm`; full suite green.
- [ ] No regressions to Fireside SOURCE/EDIT (re-run the live gates in the Tasai handoff).
- [ ] `logs/memory-log.md` + `hot.md` updated if state changes.
