"""Thai-name fact-check for NEWSROOM rewrites.

Enforces Naz's 2026-08-27 name rule on every rewrite that crosses the hub,
IDE handoff or metered alike:

  1. NO bare Thai in the body. Every Thai name appears inside square
     brackets right after its English rendering:  **English Name [ชื่อไทย]**
     The ``EN:``/``TH:`` title lines at the top of the blob are exempt.
  2. Bracketed names should exist in the vault name registry
     (``~/Cephalon/10-knowledge/name-wiki/``) — a miss is an "unverified"
     warning, so new names surface for registration.
  3. Names carry NO honorifics or titles (นาย / นาง / ranks / ตำแหน่ง) —
     titles change over time; the registry stores the bare name only.

Advisory by design (Naz's call 2026-08-27): results are surfaced in the
panel, they never block the relay. Only the CLI exits non-zero on errors,
so scripts and tests can gate on it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

THAI = "\u0e00-\u0e7f"
# A Thai run: Thai chars with single internal space runs (Thai has no
# inter-word spacing, but source text may carry spaced multi-word names).
_THAI_RUN_RE = re.compile(rf"[{THAI}]+(?:[ \t]*[{THAI}]+)*")
# A [...] group that actually contains Thai (the allowed overlay zone).
_BRACKETED_THAI_RE = re.compile(rf"\[[^\][\n]*[{THAI}][^\][\n]*\]")
_TITLE_LINE_RE = re.compile(r"^(?:EN|TH):")

# Honorific/title prefixes that must never ride inside a name overlay.
# Starter set — extends as real news teaches us (registry stays bare-name).
_HONORIFICS = (
    "นาย",
    "นางสาว",
    "นาง",
    "เด็กชาย",
    "เด็กหญิง",
    "ด.ต.",
    "ร.ต.ท.",
    "ร.ต.อ.",
    "จ.ส.ท.",
    "จ.ส.อ.",
    "พ.ต.ท.",
    "พ.ต.อ.",
    "พ.อ.",
    "พล.ต.ท.",
    "พล.ต.อ.",
    "ผอ.",
    "รมว.",
    "รมศ.",
    "ผู้ว่าฯ",
    "ปลัด",
)

DEFAULT_REGISTRY_DIR = Path.home() / "Cephalon" / "10-knowledge" / "name-wiki"


def _body(text: str) -> str:
    """Drop the EN:/TH: title pair so the Thai title line reads as exempt."""
    if text.startswith("EN:") or text.startswith("TH:"):
        parts = text.split("\n\n", 1)
        return parts[1] if len(parts) == 2 else ""
    return text


def load_registry(registry_dir: Path | None = None) -> tuple[dict[str, str], str | None]:
    """Map Thai name -> English name from name-wiki frontmatter.

    Returns (registry, error) — error is a short string when the directory
    is missing/unreadable, else None.
    """
    d = Path(registry_dir) if registry_dir else DEFAULT_REGISTRY_DIR
    try:
        files = sorted(d.glob("*.md"))
    except OSError as exc:
        return {}, f"name registry unreadable at {d}: {exc}"
    if not files and not d.exists():
        return {}, f"name registry not found at {d} — unverified lookups skipped"
    reg: dict[str, str] = {}
    for f in files:
        try:
            head = f.read_text(encoding="utf-8")[:2000]
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---", head, re.S)
        if not m:
            continue
        thai = re.search(rf'^thai:[ \t]*"?([^"\n]+)"?[ \t]*$', m.group(1), re.M)
        if not thai:
            continue
        eng = re.search(rf'^english:[ \t]*"?([^"\n]+)"?[ \t]*$', m.group(1), re.M)
        reg[thai.group(1).strip()] = eng.group(1).strip() if eng else ""
    return reg, None


def _context(text: str, start: int, end: int, span: int = 30) -> str:
    s, e = max(0, start - span), min(len(text), end + span)
    return re.sub(r"\s+", " ", text[s:e]).strip()


def check_rewritten(text: str, registry_dir: Path | None = None) -> dict:
    """Scan a rewritten blob; returns {errors, warnings, names, ok}."""
    body = _body(text or "")
    errors: list[dict] = []
    warnings: list[dict] = []

    allowed = [(m.start(), m.end()) for m in _BRACKETED_THAI_RE.finditer(body)]

    def _bracketed(s: int, e: int) -> bool:
        return any(a <= s and e <= b for a, b in allowed)

    # 1. bare Thai anywhere outside the overlay brackets -> error
    for m in _THAI_RUN_RE.finditer(body):
        if not _bracketed(m.start(), m.end()):
            errors.append(
                {"thai": m.group(0), "context": _context(body, m.start(), m.end())}
            )

    # 2/3. every overlay name: registry-verified? title-free?
    reg, reg_err = load_registry(registry_dir)
    if reg_err:
        warnings.append({"kind": "registry", "name": "", "detail": reg_err})

    verified: list[str] = []
    unverified: list[str] = []
    seen: set[str] = set()
    for a, b in allowed:
        raw = body[a + 1 : b - 1].strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        # Legacy overlay shape `English(Thai)` (2026-08-26 rule): verify the
        # THAI part against the registry, surface the readable form, and guard
        # the english spelling against the registry's verified rendering.
        english, thai_name = raw, None
        m = re.match(r"^([^()]+?)\s*\(([^()]+)\)$", raw)
        if m and re.search(rf"[{THAI}]", m.group(2)):
            english = m.group(1).strip()
            thai_name = m.group(2).strip()
        display = f"{english} ({thai_name})" if thai_name else raw
        lookup = thai_name or raw
        if lookup.startswith(_HONORIFICS):
            warnings.append(
                {
                    "kind": "honorific",
                    "name": display,
                    "detail": "name overlay carries a title/rank — strip to the bare name",
                }
            )
        if lookup in reg:
            verified.append(display)
            reg_en = reg[lookup]
            if thai_name and reg_en and reg_en.strip().lower() != english.lower():
                warnings.append(
                    {
                        "kind": "english-mismatch",
                        "name": display,
                        "detail": f'name-wiki has "{reg_en}" — bracket says "{english}"',
                    }
                )
        else:
            unverified.append(display)
            warnings.append(
                {
                    "kind": "unverified",
                    "name": display,
                    "detail": "not in name-wiki yet — verify the English form, then register",
                }
            )

    return {
        "errors": errors,
        "warnings": warnings,
        "names": {"verified": verified, "unverified": unverified},
        "ok": not errors,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Thai-name fact-check for a newsroom rewrite")
    p.add_argument("handoff", nargs="?", help="path to latest.json (rewritten/seo) — or use --text")
    p.add_argument("--text", help="check a raw rewritten blob instead of a handoff file")
    p.add_argument("--registry-dir", type=Path, default=None, help="override name-wiki dir")
    a = p.parse_args(argv)

    if a.text:
        text = a.text
    elif a.handoff:
        data = json.loads(Path(a.handoff).read_text(encoding="utf-8"))
        text = data.get("rewritten") or ""
    else:
        p.error("give a handoff path or --text")
    result = check_rewritten(text, a.registry_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
