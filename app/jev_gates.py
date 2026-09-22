"""JEV judgment gates for the NEWSROOM rewrite/CONVERT lanes (Naz, 2026-09-22).

Advisory-never-blocking, metered, cached. Jev (TypeSafe System One) supplies
the JUDGMENT regex lacks — which capitalized spans are person NAMES, and which
spans deserve broadcast emphasis markup (bold=names, underline=dates/times per
Ben's gem rules). Code owns everything else: candidates, registry lookup,
report shape. Mirrors Somatic's jev_gates pattern (built native, not ported).

Money rules:
- ONE metered call per distinct body (all candidates ride as separate
  questions in a single payload) via ~/.hermes/scripts/jev_meter.py.
- Results cached by sha256(body) — re-convert of the same text never re-bills.
- ANY failure (no meter, no key, timeout, unparseable) degrades to
  ``{"ok": True, "skipped": <reason>}`` — a rewrite never dies to Jev.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

METER = Path.home() / ".hermes" / "scripts" / "jev_meter.py"
CACHE_DIR = Path.home() / ".cache" / "railjack" / "jev_gates"
TIMEOUT_S = 60
# Bump when question wording/logic changes — stale cached verdicts must not
# survive a judging-logic upgrade.
CACHE_VERSION = "v2"

# Emphasis pick when the normalized score (0..1 across the criteria anchors)
# reaches this. Jev scores on the CRITERIA-INDEX scale (3 anchors → 0..2),
# so raw values must be normalized before any threshold.
PICK_NORMALIZED = 0.70
# Noul probability at/above this counts as a person name.
PERSON_THRESHOLD = 0.80

# ---------------------------------------------------------------- candidates

# Spans to IGNORE for name candidates: title lines, bracket overlays [Thai],
# markup interiors are fine to scan (bold names are exactly what we want),
# but already-bracketed Thai is not a candidate.
_TITLE_LINE = re.compile(r"^(?:EN|TH):.*$", re.M)
_BRACKET_THAI = re.compile(r"\[[^\]\n]*[\u0e00-\u0e7f][^\]\n]*\]")
# Capitalized-word runs — cheap NER prefilter; Jev decides person vs not.
# Allows internal particles (de, der, van, bin, del) and hyphenation.
_CAP = r"(?:[A-Z][a-z'’\-]+|[A-Z]{2,})"
_NAME_RUN = re.compile(
    rf"{_CAP}(?:(?:,| de| der| van| bin| del| al) {_CAP}|\s+{_CAP}){{0,3}}"
)
# Leading titles/ranks the overlay rule strips anyway — never part of a name.
_TITLES = {
    "prime", "deputy", "former", "acting", "mr", "mrs", "ms", "miss", "dr",
    "prof", "gen", "pol", "lt", "col", "capt", "sen", "rep", "gov", "minister",
}
# Date/time/relative-time-ish spans — emphasis (underline) candidates per the
# gem rules: explicit dates, clock times, and relative expressions.
# MOST-SPECIFIC-FIRST: alternation order decides what wins at each position
# (else the "3" in "3:00 PM" matches as a bare numeral).
_DATE_HINT = re.compile(
    r"(?:\b\d{1,2}:\d{2}\s?(?:AM|PM)?)"                                       # clock times
    r"|(?:\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s?\d{0,2})"  # month (+day)
    r"|(?:\b(?:next|last|this|early|late|mid)\s+(?:month|week|year|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b)"
    r"|(?:\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b)"
    r"|(?:\b(?:19|20)\d{2}\b)"                                                 # years
    r"|(?:\b\d{1,3}(?:,\d{3})+(?:\.\d+)?%?\b|\b\d+\b)"                         # numerals LAST
)
_BARE_DATE = re.compile(rf"(?<!~~)(?:{_DATE_HINT.pattern})(?!~~)")
_UNDERLINE = re.compile(r"~~[^~\n]*~~")
# An overlay head: a name run immediately followed by its [Thai] bracket.
_OVERLAY_TAIL = re.compile(r"\s*\[[^\]\n]*[\u0e00-\u0e7f][^\]\n]*\]")
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}


def _blocked(body: str) -> list[tuple[int, int]]:
    """Zones candidates must never come from: title lines, Thai brackets,
    ~~underlined~~ spans (already done — fragments would double-flag)."""
    zones = [(m.start(), m.end()) for m in _TITLE_LINE.finditer(body)]
    zones += [(m.start(), m.end()) for m in _BRACKET_THAI.finditer(body)]
    zones += [(m.start(), m.end()) for m in _UNDERLINE.finditer(body)]
    return zones


def _overlaps(start: int, end: int, zones: list[tuple[int, int]]) -> bool:
    return any(start < be and end > bs for bs, be in zones)


def _strip_leading_titles(span: str) -> str:
    """Drop everything through the LAST known title/rank word — title CHAINS
    ('Tourism Minister Anutin X' → 'Anutin X', 'Deputy Prime Minister Y' → 'Y')."""
    words = span.split()
    last = -1
    for i, w in enumerate(words):
        if w.lower().rstrip(",.") in _TITLES:
            last = i
    return " ".join(words[last + 1:])


def name_candidates(body: str, max_n: int = 12) -> list[str]:
    """Distinct capitalized runs in reading order (Jev says person or not).
    Skips: blocked zones, overlay heads (run already carries its [Thai]),
    month words (those are emphasis candidates, not people)."""
    zones = _blocked(body)
    seen: list[str] = []
    for m in _NAME_RUN.finditer(body):
        if _overlaps(m.start(), m.end(), zones):
            continue
        if _OVERLAY_TAIL.match(body, m.end()):
            continue  # overlay head — already carries Thai
        span = _strip_leading_titles(m.group(0).strip(" ,."))
        if len(span) > 2 and span.split()[0].lower() not in _MONTHS and span not in seen:
            seen.append(span)
        if len(seen) >= max_n:
            break
    return seen


def emphasis_candidates(body: str, max_n: int = 12) -> list[str]:
    """Bare date/time/numeral spans NOT already ~~underlined~~."""
    zones = _blocked(body)
    seen: list[str] = []
    for m in _BARE_DATE.finditer(body):
        if _overlaps(m.start(), m.end(), zones):
            continue
        s = m.group(0).strip()
        if s and s not in seen:
            seen.append(s)
        if len(seen) >= max_n:
            break
    return seen


# ---------------------------------------------------------------- payload

def _context(body: str, span: str, width: int = 110) -> str:
    """The sentence-ish window around the span's first occurrence — Jev judges
    emphasis against editorial context, not a floating number."""
    i = body.find(span)
    if i < 0:
        return span
    return body[max(0, i - width): i + len(span) + width].replace("\n", " ").strip()


def _questions(names: list[str], emphasis: list[tuple[str, str]]) -> dict:
    """One Jev call, many questions — the meter bills input tokens once.
    Emphasis questions carry sentence context (guillemets — the double-quoted
    span stays the parseable field)."""
    q: dict = {}
    for i, span in enumerate(names):
        q[f"name{i}"] = {
            "type": "noul",
            "instructions": (
                f'Is "{span}" a PERSON\'s name (a human individual, not a place, '
                "organization, brand, event, or ordinary capitalized word)?"
            ),
        }
    for i, (span, ctx) in enumerate(emphasis):
        q[f"emph{i}"] = {
            "type": "score",
            "instructions": (
                f'In this script excerpt: «{ctx}» — should "{span}" carry broadcast '
                "emphasis markup in a TV news script (0 = plain word, no markup; "
                "5 = borderline relative time or minor numeral; 10 = explicit "
                "date, clock time, or key numeral that the style rules "
                "underline/bold)?"
            ),
            "criteria": [
                "0 plain prose word — no markup",
                "5 borderline — relative/minor expression",
                "10 explicit date, time, or key numeral — markup required",
            ],
        }
    return q


def _metered_call(payload: dict) -> dict:
    """Run ONE metered Jev call; return the parsed answers dict.

    askjev exits nonzero (3 = low-confidence signal) while STILL returning the
    full answers JSON on stdout — so the exit code never decides here; only an
    unparseable stdout is an error."""
    proc = subprocess.run(
        ["python3", str(METER), "run", "payload", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    try:
        out = json.loads(proc.stdout)
    except Exception:
        raise RuntimeError(
            f"jev_meter exit {proc.returncode}, unparseable stdout: {proc.stderr.strip()[:200]}"
        )
    return out.get("answers", out)


def _cache_path(body: str) -> Path:
    digest = hashlib.sha256(f"{CACHE_VERSION}:{body}".encode()).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.json"


# ---------------------------------------------------------------- gates

def run_gates(body: str, registry: dict[str, str] | None = None) -> dict:
    """Jev-judge the body. Returns an advisory report — NEVER raises.

    names:   Jev-confirmed persons → Thai from the name-wiki registry when
             known (thai=null → flag for ＋wiki).
    emphasis: Jev-scored spans worth bolding/underlining.
    """
    body = body or ""
    names = name_candidates(body)
    emphasis = emphasis_candidates(body)
    if not names and not emphasis:
        return {"ok": True, "skipped": "no candidates", "names": [], "emphasis": []}

    cache = _cache_path(body)
    if cache.exists():
        try:
            answers = json.loads(cache.read_text())
        except Exception:
            answers = None
    else:
        answers = None
    if answers is None:
        if not METER.exists():
            return {"ok": True, "skipped": "no jev meter on this machine", "names": [], "emphasis": []}
        try:
            raw = _metered_call({
                "state": "TV news rewrite advisory gates (names + emphasis)",
                "questions": _questions(names, [(s, _context(body, s)) for s in emphasis]),
            })
            answers = raw.get("answers", raw)
        except Exception as exc:
            return {"ok": True, "skipped": f"jev call failed: {exc}"[:160], "names": [], "emphasis": []}
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(answers))
        except Exception:
            pass  # cache is best-effort; judgment already made

    registry = registry or {}
    out_names = []
    for i, span in enumerate(names):
        a = answers.get(f"name{i}") or {}
        prob = a.get("noul")  # askjev keys the probability by answer type
        choice = str(a.get("choice", a.get("answer", ""))).lower()
        is_person = prob >= PERSON_THRESHOLD if isinstance(prob, (int, float)) else choice in ("yes", "true")
        if not is_person:
            continue
        out_names.append({
            "english": span,
            "thai": registry.get(span.lower()) or registry.get(span),
            "context": body[max(0, body.find(span) - 40): body.find(span) + len(span) + 40].strip(),
        })
    out_emph = []
    n_anchors = 3  # criteria count in _questions — score scale is 0..n-1
    for i, span in enumerate(emphasis):
        a = answers.get(f"emph{i}") or {}
        score = a.get("score")
        if not isinstance(score, (int, float)):
            continue
        norm = score / (n_anchors - 1)
        if norm < PICK_NORMALIZED:
            continue
        out_emph.append({"span": span, "score": round(score, 2), "pick": round(norm, 2)})
    return {"ok": True, "names": out_names, "emphasis": out_emph}


def load_registry_map() -> dict[str, str]:
    """english → thai from the vault name-wiki (inverts name_check.load_registry,
    which already owns the frontmatter parsing)."""
    from .name_check import load_registry

    reg, _err = load_registry()
    return {en.lower(): th for th, en in reg.items() if en}
