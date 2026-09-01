"""Ben-voice / anti-slop linter for NEWSROOM rewrites (rule 14 as code).

Ben's gem rule 14 was distilled from the unslop skill
(``skill-library/skills/unslop``); this module distills it one step further —
into checks a script can run on every rewrite that crosses the hub, IDE
handoff or metered alike:

  ERRORS — objective violations, no judgment call:
    em/en dashes · prose parentheses (parens INSIDE Thai-bearing overlay
    brackets are functional formatting — exempt) · unbalanced ** / ~~
    markers · bullets, headings, links, backticks in the body (rule 6 allows
    only the ** and ~~ markers).

  WARNINGS — heuristic nudges, human arbitrates:
    AI vocabulary / puffery (rule 14 list + unslop skill), sentences over
    ~25 words, body outside 180-250 words (gem rule 7), paragraphs outside
    2-4 (rule 6), first person in the body, curly quotes.

What deliberately stays prompt-side: voice quality, the lede's
macro-vs-anecdote judgment, adjective-stacking precision, forced triads —
they need a reader, not a regex. Advisory by design (same call as the name
hook): results ride rewrite/CONVERT responses, they never block. Only the
CLI exits non-zero on errors, so the IDE lane and tests can gate on it.

Railjack-native companion to ``app.name_check`` (2026-08-29 port of Tasai's
Somatic build ``209aec4``).
"""

from __future__ import annotations

import json
import re
import sys

from .name_check import _body

_DASH_RE = re.compile(r"[—–]")
_PAREN_RE = re.compile(r"[()]")
_CURLY_RE = re.compile(r"[\u201c\u201d\u2018\u2019]")
# Rule 6: continuous prose only — no bullets, numbered items, subheadings,
# links, or code ticks. The ** and ~~ markers are the ONLY allowed markup.
_BAD_MARKUP_RE = re.compile(r"(?m)^\s*(?:[-*•+]\s+|\d+[.)]\s+|#{1,6}\s+)|\]\(|`")

# The overlay zone: bracket groups containing Thai (legacy `[English(ไทย)]` and
# current `[English [ไทย]]` shapes). Parens inside are functional formatting.
_BRACKET_RE = re.compile(r"\[[^\][\n]*\]")
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")

# AI vocabulary, puffery, and grade-words — rule 14's list plus the unslop
# skill's top offenders. ponytail: starter set, extends as anchors complain;
# source of truth stays the gem + skill-library/skills/unslop.
_SLOP_PATTERNS = [
    (r"\bnot\s+just\b[^.!?]{0,60}\bbut\b", '"not just X, but Y" — state the point'),
    (r"\bnot\s+only\b[^.!?]{0,60}\bbut\b", '"not only ... but" — state the point'),
    (r"\b(serve|stand)s\s+as\b", 'fancy "is" — say "is"'),
    (
        r"\b(?:pivotal|testament|underscor\w+|showcas\w+|delv\w+|tapestry|landscape"
        r"|vibrant|breathtaking|groundbreaking|renowned|crucial|striking|remarkable"
        r"|dramatic|massive|nestled|must-visit|elevat\w+|foster\w*|garner\w*"
        r"|utilize\w*|leverage\w*|facilitat\w+|numerous|boasts?\w*)\b",
        "AI vocabulary / grade-word — use the plain or sourced word",
    ),
    (r"\bin order to\b", 'filler — "to"'),
    (r"\bdue to the fact that\b", 'filler — "because"'),
    (r"\bit is important to note that\b", "filler — delete"),
]
_SLOP_RES = [(re.compile(p, re.I), hint) for p, hint in _SLOP_PATTERNS]

# Spelled-out quantities — Naz 2026-08-31: "69 is easier to read than sixty nine".
# Flags number-word compounds (sixty thousand, two hundred eighty thousand, sixty-nine).
# Single bare number words ("fifteen years") stay unflagged — too noisy near names/idioms;
# numeral+magnitude ("3.2 million") is gem-correct and never matches (digits, not words).
_NUMWORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
            r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
            r"fifty|sixty|seventy|eighty|ninety)")
_SPOKEN_NUMBER_RE = re.compile(
    r"\b" + _NUMWORD + r"(?:[-\s]" + _NUMWORD + r")*\s+(?:hundred|thousand|million|billion)\b"
    r"|\b(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[-\s]"
    r"(?:one|two|three|four|five|six|seven|eight|nine)\b",
    re.I)

_MAX_SENTENCE_WORDS = 25
_WORD_RANGE = (180, 250)
_PARAGRAPH_RANGE = (2, 4)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def check_style(text: str) -> dict:
    """Scan a rewritten blob; returns ``{errors, warnings, stats, ok}``."""
    body = _body(text or "")
    errors: list[dict] = []
    warnings: list[dict] = []

    def _flag(bucket: list[dict], kind: str, detail: str) -> None:
        if not any(e["kind"] == kind for e in bucket):
            bucket.append({"kind": kind, "detail": detail})

    # 1. objective errors
    if _DASH_RE.search(body):
        n = len(_DASH_RE.findall(body))
        _flag(errors, "dash", f"{n} em/en dash(es) — rule 14: periods and commas only")
    overlay_spans = [
        (m.start(), m.end()) for m in _BRACKET_RE.finditer(body) if _THAI_RE.search(m.group(0))
    ]

    def _in_overlay(pos: int) -> bool:
        return any(a <= pos < b for a, b in overlay_spans)

    prose_parens = [m for m in _PAREN_RE.finditer(body) if not _in_overlay(m.start())]
    if prose_parens:
        _flag(errors, "paren", "parentheses — rule 14: the anchor reads aloud, needs breath points")
    if _BAD_MARKUP_RE.search(body):
        _flag(errors, "markup", "bullets/heading/link/backtick in body — rule 6: prose only, ** and ~~ markers aside")
    for marker, name in (("**", "bold"), ("~~", "underline")):
        if body.count(marker) % 2:
            _flag(errors, "markers", f"unbalanced {marker} {name} markers — the Doc renderer will garble them")
    if _CURLY_RE.search(body):
        _flag(warnings, "quotes", "curly quotes in body — use straight quotes")

    # 2. slop vocabulary / constructions
    for pattern, hint in _SLOP_RES:
        m = pattern.search(body)
        if m:
            warnings.append({"kind": "slop", "name": m.group(0), "detail": hint})

    # 2b. spelled-out quantities — numerals only (69, not "sixty nine")
    for m in _SPOKEN_NUMBER_RE.finditer(body):
        warnings.append({
            "kind": "number",
            "name": m.group(0),
            "detail": "write numbers as numerals — 60,000, not 'sixty thousand' (69, not 'sixty-nine')",
        })

    # 3. rhythm + shape heuristics
    sents = _sentences(body)
    long_ones = [len(s.split()) for s in sents if len(s.split()) > _MAX_SENTENCE_WORDS]
    if long_ones:
        warnings.append(
            {
                "kind": "long-sentence",
                "name": "",
                "detail": f"{len(long_ones)} sentence(s) over ~{_MAX_SENTENCE_WORDS} words (longest {max(long_ones)}) — split or drop a clause",
            }
        )

    words = len(body.split())
    paras = [p for p in body.split("\n\n") if p.strip()]
    if not _WORD_RANGE[0] <= words <= _WORD_RANGE[1]:
        warnings.append(
            {
                "kind": "word-count",
                "name": "",
                "detail": f"body is {words} words — gem rule 7 wants {_WORD_RANGE[0]}-{_WORD_RANGE[1]} (under-length is the worse failure)",
            }
        )
    if not _PARAGRAPH_RANGE[0] <= len(paras) <= _PARAGRAPH_RANGE[1]:
        warnings.append(
            {
                "kind": "paragraphs",
                "name": "",
                "detail": f"{len(paras)} paragraphs — rule 6 wants {_PARAGRAPH_RANGE[0]}-{_PARAGRAPH_RANGE[1]}",
            }
        )
    first_person = re.findall(r"\b(?:we|our|us)\b", body, re.I)
    if first_person:
        warnings.append(
            {
                "kind": "first-person",
                "name": "",
                "detail": f"{len(first_person)} first-person pronoun(s) — broadcast copy stays third person",
            }
        )

    return {
        "errors": errors,
        "warnings": warnings,
        "stats": {"words": words, "paragraphs": len(paras), "sentences": len(sents)},
        "ok": not errors,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    p = argparse.ArgumentParser(description="Ben-voice / anti-slop check for a newsroom rewrite")
    p.add_argument("handoff", nargs="?", help="path to latest.json (rewritten/seo) — or use --text")
    p.add_argument("--text", help="check a raw rewritten blob instead of a handoff file")
    a = p.parse_args(argv)

    if a.text:
        text = a.text
    elif a.handoff:
        data = json.loads(Path(a.handoff).read_text(encoding="utf-8"))
        text = data.get("rewritten") or ""
    else:
        p.error("give a handoff path or --text")
    result = check_style(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
