# AEO Proposal — Thailand NOW as the source AI engines cite

**For:** Ben (editor sign-off needed) · **From:** the Railjack night shift · **Date:** 2026-10-02

## What AEO is

Answer Engine Optimization: making Thailand NOW the source that ChatGPT, Perplexity,
Google AI Overviews and Copilot **cite** when people ask about Thailand — travel,
culture, business, events. Search is shifting from "10 blue links" to "one answer
with sources". The sites that get cited win the traffic that used to go to rank #1.

## Why Thailand NOW is well-positioned

- Original reporting and a structured events database — exactly what answer engines
  want to cite (primary sources over aggregators).
- Clean WordPress + Yoast foundation already in place.
- The Railjack SEO feature now audits health continuously — AEO slots into the same panel.

## What we would build (the audit layer — no content changes without editorial sign-off)

1. **Per-page AEO checks in the SEO HEALTH scan**: JSON-LD schema present
   (NewsArticle / Event), FAQ or Q&A block present, heading hierarchy (one H1,
   ordered H2/H3), direct-answer paragraph near the top, canonical + author byline.
2. **llms.txt** — a generated index of TN's best evergreen guides at
   `/llms.txt` (the emerging convention AI crawlers read first).
3. **AEO score per page** in the SEO panel, grouped like the current error sections,
   with a suggested fix per gap.
4. **Cite tracking** — a periodic ask to one AI engine ("who covers X?") logged over
   time, so we can see TN's citation share move.

## What editorial would own (the content program)

- Answer-first intros on evergreen guides (2–3 sentence direct answers).
- 2–3 Q&A pairs on big guides (FAQ blocks with schema).
- Author bylines + bios on everything (E-E-A-T).

## Effort

Audit layer: ~1 build pass in the existing SEO feature (similar size to the
oversized-image toolkit). Content practices: ongoing, writer-side, no new tools.

## The ask

Green light on the audit layer build + the editorial practices above. No site
changes go live without editorial review.
