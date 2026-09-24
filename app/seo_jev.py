"""JEV gates for THAILAND NOW SEO link operations (Naz, 2026-09-23).

BULK LINK ALL ships anchor links into live WordPress at scale — the one
place a bad phrase writes en masse. Jev (via the shared metered plumbing in
``jev_gates``) judges each would-be link twice:

  pairing — is HOST a natural context to link TARGET from?   (noul)
  anchor  — is PHRASE a natural anchor for TARGET there?     (noul)

Gating hardness (Naz, grill round 1): bulk runs AUTO-SKIP JEV-failed pairs;
single / manual ops (preview-insert) surface advisory verdicts only.
Degrades to no-gate on ANY JEV failure — an operation never dies to Jev.
Verdict caches ride the shared model-stamped store, so a JEV upgrade
flushes them and the next run re-judges on the new model (jev_gates v5).
"""

from __future__ import annotations

from .jev_gates import (
    CACHE_TTL_S,  # noqa: F401 — re-exported for tests / callers
    flush_cache,
)

# A pair/anchor passes the bulk gate at this noul. Same live-tuned scale as
# PERSON_THRESHOLD: regex/LLM-prefiltered candidates put real picks ≥ ~0.6
# and noise far below.
GATE_THRESHOLD = 0.60

_STATE = "THAILAND NOW SEO internal-linking gate: orphan pages gaining one contextual link each from suggested host articles."


def gate_bulk_picks(orphan_title: str, orphan_link: str,
                    picks: list[dict]) -> tuple[dict[str, bool], dict]:
    """Jev-judge a dry-run pick set for one orphan.

    ``picks``: the ``results`` list of ``_seo_bulk_link_orphan(dry_run=True)`` —
    entries carrying ``host_id`` + a picked ``phrase`` (noop/failed entries
    pass through ungated; there is nothing to write).

    Returns ``(map, meta)``: map is ``{host_id: passed}`` for PICKED hosts
    only (absent = ungated → caller treats as pass); meta carries
    ``model``/``skipped`` for the report. NEVER raises — any JEV failure
    degrades to an empty map (no gate).
    """
    picked = [p for p in picks if p.get("phrase")]
    if not picked:
        return {}, {"skipped": "no picks to gate"}
    try:
        from .jev_gates import cache_read, cache_write, metered_questions

        key = "seo-bulk:" + orphan_link + ":" + ",".join(
            f"{p['host_id']}:{p['phrase']}" for p in picked)
        answers, old_model = cache_read(key)
        model = old_model  # cache hit → this IS the judging model
        if answers is None:
            questions = {}
            for i, p in enumerate(picked):
                questions[f"pair{i}"] = {
                    "type": "noul",
                    "instructions": (
                        f'Is the article "{p.get("host_title", "")}" a natural context '
                        f'to link the page "{orphan_title}" ({orphan_link}) from? '
                        "Answer no if the topics are unrelated or the link would "
                        "look forced there."),
                }
                questions[f"anchor{i}"] = {
                    "type": "noul",
                    "instructions": (
                        f'In that host article, is the phrase "{p["phrase"]}" a natural, '
                        f'readable anchor text for a link to "{orphan_title}"? '
                        "Answer no if the phrase reads as spammy, off-topic, or "
                        "would confuse a reader who clicks it."),
                }
            answers, model = metered_questions(_STATE, questions)
            # JEV upgraded since these verdicts were cached? Flush the shared
            # store so every consumer re-judges on the new model (v5 law).
            if model and old_model and model != old_model:
                flush_cache()
            cache_write(key, answers, model)
        out: dict[str, bool] = {}
        for i, p in enumerate(picked):
            pair = (answers.get(f"pair{i}") or {}).get("noul")
            anchor = (answers.get(f"anchor{i}") or {}).get("noul")
            pair_ok = pair >= GATE_THRESHOLD if isinstance(pair, (int, float)) else True
            anchor_ok = anchor >= GATE_THRESHOLD if isinstance(anchor, (int, float)) else True
            out[str(p["host_id"])] = bool(pair_ok and anchor_ok)
        return out, {"model": model, "gated": len(picked)}
    except Exception as exc:  # noqa: BLE001 — an op never dies to Jev
        return {}, {"skipped": f"jev gate unavailable: {exc}"[:120]}


def verdict_insert(phrase: str, orphan_title: str, host_title: str,
                   host_excerpt: str) -> dict:
    """Advisory JEV verdict for a MANUAL insert (preview-insert). Never
    raises; ``{"verdict": "ok"|"weak"|"skipped", "prob": x, "model": m}``."""
    try:
        from .jev_gates import cache_read, cache_write, metered_questions

        key = f"seo-insert:{orphan_link_key(orphan_title, phrase, host_title)}"
        answers, old_model = cache_read(key)
        model = old_model  # cache hit → this IS the judging model
        if answers is None:
            questions = {"ins0": {
                "type": "noul",
                "instructions": (
                    f'On the page "{host_title}" — excerpt: «{host_excerpt[:600]}» — '
                    f'would the phrase "{phrase}" work as a natural, readable inline '
                    f'link anchor pointing to the page "{orphan_title}"? Answer no if '
                    "it reads spammy, off-topic, or mismatched to that target.")}}
            answers, model = metered_questions(_STATE, questions)
            if model and old_model and model != old_model:
                flush_cache()
            cache_write(key, answers, model)
        prob = (answers.get("ins0") or {}).get("noul")
        if not isinstance(prob, (int, float)):
            return {"verdict": "skipped", "model": model}
        return {"verdict": "ok" if prob >= GATE_THRESHOLD else "weak",
                "prob": round(prob, 2), "model": model}
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "skipped", "detail": str(exc)[:120]}


def orphan_link_key(orphan_title: str, phrase: str, host_title: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", f"{orphan_title} {phrase} {host_title}".lower())[:120]
