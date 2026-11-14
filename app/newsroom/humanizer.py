"""Humanizer pass for the NEWSROOM send paths (SEND TO NL / SEND TO RADIO).

Port of O's Somatic design (Cephalon hot.md 2026-11-04), implemented natively
on Railjack — not copied. Every Doc write runs the script through the
humanizer (agy, model id from [[app/platform/agy.py]][AGY_MODEL], embedded
mode) first, always-on and
fail-fast: service down or unverifiable output → 502, nothing written.

Structural pieces the Doc pipeline depends on — **name** bold markers,
~~date~~ underline markers, Thai-script overlay runs, the headline line — are
masked to visible ``[M0]``-style placeholders before the model sees the text
and restored byte-exact after. Visible ASCII brackets survive the gemini
tokenizer; private-use sentinels do not (O's lesson, 2026-11-04).
"""

import re

from fastapi import HTTPException

from app.platform.agy import AGY_MODEL
from app.thailand_now.scout import _agy_complete

_MODEL = AGY_MODEL
_TIMEOUT = 300.0
# Below this, humanizing is noise: short fills are already lean.
_MIN_LEN = 200

_THAI_RUN = re.compile(r"[ก-๙][ก-๙\s]*")
_BOLD = re.compile(r"\*\*[^*]+\*\*")
_UNDER = re.compile(r"~~[^~]+~~")
_PLACEHOLDER = re.compile(r"\[M\d+\]")
# British/UN "per cent" → "percent" so the broadcast formatter's % regex sees one token.
_PCT_RE = re.compile(r"\bper cent\b", re.IGNORECASE)


def _normalize_percent(text: str) -> str:
    return _PCT_RE.sub("percent", text)


_PROMPT = (
    "You are a copy editor at a broadcast newsroom. Rewrite the TEXT below so "
    "it no longer reads like AI-generated prose. Do not change any fact.\n"
    "Remove these patterns, strongest first:\n"
    "1. \"not X but Y\" contrast framing, and one-line closers that restate the point\n"
    "2. staged openings (\"In a significant development\", \"This marks a milestone\")\n"
    "3. forced triads — three parallel items where one or two carry the meaning\n"
    "4. em-dash chains where a period serves\n"
    "5. inflation words: pivotal, underscores, testament to, backbone, vibrant, landscape\n"
    "6. bold labels inside sentences and title-case fragments\n"
    "Keep every fact, number, date, name and claim, in the same order. Add nothing. "
    "Do not summarize. Keep the paragraph count similar.\n"
    "Write percentages as the single word 'percent' — never the two-word 'per cent'.\n"
    "Tokens like [M0], [M1] are immutable placeholders — keep each one in place, "
    "exactly once, never renumber, merge, or reformat them.\n"
    "Return ONLY the rewritten text — no preamble, no notes, no quotation marks."
)


def mask_structures(text: str) -> tuple[str, dict[str, str]]:
    """Replace structural segments with ``[M#]`` placeholders.

    Order matters: the headline line goes first (its inner bold/Thai rides
    inside one placeholder), then bold, then underline, then bare Thai runs.
    Returns ``(masked, table)`` — ``table`` maps placeholder → original.
    """
    table: dict[str, str] = {}

    def put(m: re.Match) -> str:
        key = f"[M{len(table)}]"
        table[key] = m.group(0)
        return key

    out = text
    lines = out.split("\n")
    if len(lines) >= 2 and 1 <= len(lines[0]) <= 100 and not lines[0].rstrip().endswith((".", "!", "?", "।")):
        out = put(re.match(r"[^\n]+", out)) + "\n" + out.split("\n", 1)[1]
    out = _BOLD.sub(put, out)
    out = _UNDER.sub(put, out)
    out = _THAI_RUN.sub(put, out)
    return out, table


def placeholders_ok(masked_out: str, table: dict[str, str]) -> bool:
    """Every placeholder present exactly once; no unknown ``[M#]`` tokens."""
    found = _PLACEHOLDER.findall(masked_out)
    return sorted(found) == sorted(table.keys())


def unmask(masked_out: str, table: dict[str, str]) -> str:
    """Restore every placeholder to its original segment, byte-exact.

    Raises ``KeyError`` on a stray placeholder — the caller treats that as a
    failed verification (the model invented or mangled a token)."""
    def sub(m: re.Match) -> str:
        key = m.group(0)
        if key not in table:
            raise KeyError(key)
        return table[key]
    return _PLACEHOLDER.sub(sub, masked_out)


async def humanize_text(text: str) -> str:
    """Humanize broadcast-script text before a Doc write. Fail-fast: raises
    502 when agy is unreachable or the output fails verification twice —
    the caller writes nothing rather than silently skipping the pass."""
    if len(text) < _MIN_LEN:
        return text
    masked, table = mask_structures(_normalize_percent(text))
    prompt = _PROMPT + "\n\n=== TEXT ===\n" + masked
    for _attempt in (1, 2):
        out = await _agy_complete(prompt, model=_MODEL, timeout=_TIMEOUT)
        if not out:
            continue  # agy down/flake → retry, then 502 below
        out = out.strip()
        if placeholders_ok(out, table):
            try:
                return _normalize_percent(unmask(out, table))
            except KeyError:
                continue
    raise HTTPException(
        502,
        "humanizer unavailable or output failed verification after retry — nothing written",
    )
