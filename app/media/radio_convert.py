"""RADIO ▸ News Fill — Convert step (split from Apply, Naz 2026-11-05).

Two-button contract:
  1. ``CONVERT SELECTED`` — runs all text work per ticked piece:
     4-pass rewrite -> humanizer deep-edit -> figure-verify -> explicit
     ``title`` + ``body`` split. Results are persisted by URL in
     ``~/.config/railjack/radio_converted/<sha256[:16]>.json`` so re-ticks,
     retries, and hub restarts never re-burn LLM tokens.
  2. ``CONFIRM & APPLY`` — streaming loop (`radio_loop.py`) or strict
     cache-gated ``apply_converted``: plans placement against the live doc
     and writes via ``radio_news.py fill --explicit``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from app.media import radio_news
from app.newsroom.humanizer import humanize_text
from app.newsroom.name_check import THAI, check_rewritten, load_registry
from app.newsroom.newsroom import _gem_text, _strip_fabricated_thai
from app.newsroom.style_check import check_style
from app.platform import jev_gates, zai

router = APIRouter()

# Module-level alias so callers/tests can reference either ``humanize`` or ``humanize_text``.
humanize = humanize_text

CACHE_DIR = Path.home() / ".config" / "railjack" / "radio_converted"
# Follow DSH's configured agent-default-model (resolved per call in zai.py).
CONVERT_MODEL = "dsh"

# Title lint thresholds — radio headlines are short slug lines, never sentences.
TITLE_MAX_WORDS = 16
TITLE_LEDE_WORDS = 12
_HEADLINE_PREFIX_RE = re.compile(r"^(?:HEADLINE|TITLE)\s*:\s*", re.I)
_TITLE_LINE_RE = re.compile(r"^(?:EN|TH)\s*:")
_BARE_THAI_RE = re.compile(rf"(?<!\[)[{THAI}]+(?:\s+[{THAI}]+)*(?!\])")

# Relative-day tokens banned by Editor Ben's rule 14 ("never date events as
# today / this morning / yesterday").
_FRESHNESS_RE = re.compile(
    r"\b(?:today|tonight|yesterday|this\s+(?:morning|afternoon|evening|week))\b",
    re.I,
)

# Sentence-level figure patterns for JEV figure verification.
_FIG_PAT = re.compile(
    r"(?:"
    r"[\$€£¥฿]\s?\d[\d,.]*(?:\s?(?:million|billion|trillion|m|bn|k))?"
    r"|\b\d[\d,.]*\s?(?:%|percent|per\s+cent|percentage\s+points?|basis\s+points?|bps)\b"
    r"|\b\d[\d,.]*\s+(?:million|billion|trillion|thousand|hundred)\b"
    r"|\b\d[\d,.]*\s+(?:people|dead|killed|injured|wounded|arrested|troops|soldiers|"
    r"workers|jobs|tonnes|tons|barrel[s]?|km|miles|meters|metres|hectares|acres|"
    r"years|months|days|hours|flights|ships|vessels|aircraft|missiles|drones|"
    r"seats|votes|cases|deaths|patients|schools|hospitals|homes|buildings|"
    r"companies|firms|countries|nations|states|provinces|baht|dollars|euros|yen|yuan)\b"
    r")",
    re.I,
)
_NUM_TOKEN = re.compile(
    r"[\$€£¥฿]?\d[\d,.]*(?:\s?(?:million|billion|trillion|thousand|%|percent))?",
    re.I,
)

_PASS1_SYSTEM = (
    "You are a broadcast-news translator and condenser. Convert the raw source "
    "article below into plain, factual English sentences covering all key facts, "
    "figures, dates, and names. Keep any Thai person/place script in brackets "
    "next to its English rendering if present in the source; never invent Thai "
    "script. Output plain paragraphs only."
)

# Radio adaptation appended to Ben's rules in Pass 3: radio slots have no
# EN:/TH: title pair, no ~~date~~ underline markers, and no AI SEO block.
_RADIO_PASS3_TAIL = """

=== RADIO SLOT ADAPTATION (OVERRIDES TITLE PAIR / MARKERS / SEO) ===
This script is for an English-language radio news slot:
- Output ONE short broadcast headline on the first line (<= 12 words, plain text, NO "HEADLINE:" or "EN:" prefix, no period at the end — NEVER the intro sentence), then a blank line, then the 2-4 body paragraphs.
- Total body length MUST be 180-250 words across 2-4 paragraphs separated by blank lines.
- Do NOT emit a TH: line, do NOT emit ~~date~~ or **bold** markers (the downstream formatter handles styling), and do NOT emit an AI SEO BLOCK.
- Keep every number, percentage, currency amount, date, and proper name EXACTLY as in the source/rough text. Write percentages as 'percent', never 'per cent'.
"""


# ---------------------------------------------------------------- helpers


def freshness_scan(text: str) -> list[dict]:
    """Return relative-day rule-14 hits in ``text`` (advisory list)."""
    hits: list[dict] = []
    for m in _FRESHNESS_RE.finditer(text or ""):
        hits.append({"token": m.group(0), "pos": m.start()})
    return hits


def mech_checks(text: str) -> dict:
    """Run deterministic name + style checks on ``text``."""
    return {
        "namecheck": check_rewritten(text),
        "stylecheck": check_style(text),
    }


async def pass1_rough(source: str, model: str | None = CONVERT_MODEL) -> str:
    """Pass 1 — rough factual condensation/translation of the source article."""
    out = await zai.zai_message(
        f"=== SOURCE ARTICLE ===\n{source.strip()}",
        max_tokens=1200,
        system=_PASS1_SYSTEM,
        model=model,
        timeout=90.0,
    )
    if not out:
        raise HTTPException(502, "Pass 1 (rough) returned empty text")
    return out


def _split_sents(text: str) -> list[str]:
    return [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
        if s.strip()
    ]


def verify_figures(source: str, rewritten: str) -> dict:
    """Compare figure-bearing sentences in ``rewritten`` against ``source``.

    Uses JEV when the local meter is available; degrades cleanly to
    ``{"ok": True, "skipped": ..., "mismatches": []}`` when offline or unconfigured.
    """
    sents = [s for s in _split_sents(rewritten) if _FIG_PAT.search(s)][:8]
    if not sents:
        return {"ok": True, "checked": 0, "mismatches": []}
    if not jev_gates.METER.exists():
        return {"ok": True, "skipped": "no jev meter", "mismatches": []}

    src_nums = ", ".join(dict.fromkeys(m.group(0) for m in _NUM_TOKEN.finditer(source or "")))
    state = f"SOURCE TEXT:\n{(source or '')[:3500]}\n\nNUMBERS IN SOURCE: {src_nums or '(none)'}"
    questions: dict = {}
    for i, s in enumerate(sents):
        figs = ", ".join(m.group(0) for m in _FIG_PAT.finditer(s))
        questions[f"s{i}"] = {
            "type": "score",
            "instructions": (
                f'Rewrite sentence: "{s}" (figures: {figs}). '
                "Compare every number, count, percentage, currency amount, unit, and "
                "timeframe in this sentence against SOURCE TEXT. Does every figure match "
                "the source (exact value or faithful rounding of a source number)?"
            ),
            "criteria": [
                "0 mismatch: a number, unit, percentage, currency, or timeframe contradicts the source or is invented (not in the source)",
                "5 minor rounding of a figure that IS in the source, meaning preserved",
                "10 exact match: every figure, unit, and timeframe matches the source",
            ],
        }
    try:
        answers, _model = jev_gates.metered_questions(state, questions)
    except Exception as exc:
        return {"ok": True, "skipped": f"jev error: {exc}"[:160], "mismatches": []}

    mismatches: list[dict] = []
    for i, s in enumerate(sents):
        a = (answers or {}).get(f"s{i}") or {}
        raw = a.get("score")
        norm = (raw / 2.0) if isinstance(raw, (int, float)) else 1.0
        figs = [m.group(0) for m in _FIG_PAT.finditer(s)]
        if norm < 0.7:
            mismatches.append({"sentence": s, "figures": figs, "score": round(norm, 3)})
    return {
        "ok": len(mismatches) == 0,
        "checked": len(sents),
        "mismatches": mismatches,
    }


def verify_block(vf: dict) -> str:
    """Render a visible ``[VERIFY — ...]`` prepend block when figures mismatch."""
    if not vf or vf.get("ok", True):
        return ""
    parts = [
        f'"{m.get("sentence", "")}" (check: {", ".join(m.get("figures") or [])})'
        for m in (vf.get("mismatches") or [])
    ]
    if not parts:
        return ""
    return "[VERIFY — figures do not match source: " + "; ".join(parts) + "]"


async def _run_humanize(text: str) -> tuple[str, dict]:
    """Invoke ``humanize`` (defaults to ``app.newsroom.humanizer.humanize_text``).

    Supports both Railjack's async ``humanize_text(text) -> str`` and test
    monkeypatches returning ``(text, meta)`` or ``str`` synchronously.
    """
    fn = globals().get("humanize") or humanize_text
    try:
        res = fn(text)
        if inspect.isawaitable(res):
            res = await res
        if isinstance(res, tuple) and len(res) == 2:
            return str(res[0]), dict(res[1])
        return str(res), {"ok": True}
    except Exception as exc:
        return text, {"ok": False, "skipped": f"humanizer error: {exc}"[:160]}


# ---------------------------------------------------------------- cache


def _url_key(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()[:16]


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{_url_key(url)}.json"


def cache_get(url: str) -> dict | None:
    """Return a valid cached convert record for ``url``, or ``None``."""
    if not url:
        return None
    p = _cache_path(url)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        if d.get("url") == url and d.get("title") and d.get("body"):
            return d
    except Exception:
        return None
    return None


def cache_put(entry: dict) -> None:
    """Persist a converted piece record to ``CACHE_DIR``."""
    url = entry.get("url") or ""
    if not url:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(url).write_text(json.dumps(entry, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- title/body split + lint


def split_title_body(text: str, fallback_title: str = "") -> tuple[str, str, list[str]]:
    """Split rewritten text into ``(title, body, title_problems)``.

    The rewrite prompt asks for ``<headline>\\n\\n<body paragraphs>``. When the
    model omits the headline and starts directly with the lede sentence (>16
    words), line 1 is demoted back into the body and ``fallback_title``
    (the scout's headline) is promoted so a paragraph never lands in the Doc's
    H2 ``N.[Title]`` slot.
    """
    problems: list[str] = []
    paras = [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    if not paras:
        return (fallback_title or "Untitled").strip(), "", ["empty rewrite"]

    first_lines = paras[0].split("\n", 1)
    raw_first = first_lines[0].strip()

    had_prefix = bool(_HEADLINE_PREFIX_RE.match(raw_first))
    cleaned_first = _HEADLINE_PREFIX_RE.sub("", raw_first).strip().strip("*").strip()
    if had_prefix:
        problems.append("prefix (HEADLINE:/TITLE: stripped)")

    words = cleaned_first.split()
    is_long = len(words) > TITLE_MAX_WORDS
    is_lede = len(words) > TITLE_LEDE_WORDS and cleaned_first.endswith(".")
    if is_long:
        problems.append(f"long ({len(words)}w > {TITLE_MAX_WORDS}w)")
    if is_lede:
        problems.append("looks like lede sentence")

    # Enforced demotion: if line 1 is >16 words, keep it in the body and use
    # fallback_title as the headline.
    if is_long:
        fb = _HEADLINE_PREFIX_RE.sub("", (fallback_title or "").strip()).strip()
        title = fb or " ".join(words[:12])
        body_paras = [
            _HEADLINE_PREFIX_RE.sub("", paras[0]).strip(),
            *paras[1:],
        ]
        problems.append("demoted (fallback title used)")
        return title, "\n\n".join(p for p in body_paras if p), problems

    # Normal case: paras[0] (or its first line) is the headline.
    title = cleaned_first
    rest_first = first_lines[1].strip() if len(first_lines) > 1 else ""
    body_paras = ([rest_first] if rest_first else []) + paras[1:]
    body = "\n\n".join(p for p in body_paras if p)

    if not title:
        problems.append("missing")
        title = (fallback_title or "Untitled").strip()

    if not body:
        # Single-paragraph output — keep it as body and promote fallback title.
        body = title
        title = (fallback_title or " ".join(words[:10]) or "Untitled").strip()
        problems.append("single-block (fallback title used)")

    return title, body, problems


# ---------------------------------------------------------------- 4-pass + humanize + verify


def _build_radio_pass3_prompt(
    rough: str,
    source: str,
    names: dict[str, str],
    src_title: str,
) -> str:
    gem = _gem_text() + _RADIO_PASS3_TAIL
    names_block = (
        "\n".join(f"  {th} -> {en}" for th, en in names.items())
        if names
        else "  (none)"
    )
    return (
        f"{gem}\n\n"
        f"=== VERIFIED THAI->ENGLISH NAME MAP ===\n{names_block}\n\n"
        f"=== SOURCE HEADLINE (context only — write a fresh <=12-word radio headline) ===\n{src_title}\n\n"
        f"=== ROUGH ENGLISH TEXT ===\n{rough.strip()}\n\n"
        f"=== ORIGINAL SOURCE (facts/figures anchor) ===\n{source.strip()}"
    )


async def convert_piece(piece: dict, model: str = CONVERT_MODEL) -> dict:
    """Run the full per-piece Convert pipeline and persist to ``CACHE_DIR``.

    Steps:
      1. Pass 1 — rough translation/condensation.
      2. Pass 2 — Thai name registry lookup.
      3. Pass 3 — Editor Ben's broadcast voice adapted for radio slots.
      4. Pass 4 — fabricated-Thai strip + mechanical checks + freshness scan.
      5. Pass 5 — Humanizer deep-edit (``app.newsroom.humanizer.humanize_text``).
      6. Pass 6 — Figure verification against source (``[VERIFY]`` banner on mismatch).
      7. Split + lint into explicit ``title`` and ``body``.
    """
    url = str(piece.get("url") or "").strip()
    src_title = str(piece.get("title") or "").strip()
    source = str(piece.get("content") or piece.get("text") or "").strip()
    if len(source) < 40:
        raise ValueError(f"piece content too short ({len(source)} chars)")

    passes: list[dict] = []

    # Pass 1 — rough
    rough = await pass1_rough(source, model=model)
    passes.append({"pass": "pass1_rough", "ok": True, "chars": len(rough)})

    # Pass 2 — Thai names from vault registry (read-only)
    reg, _ = load_registry()
    found_thai = {
        m.group(0).strip()
        for m in _BARE_THAI_RE.finditer(rough)
        if not _TITLE_LINE_RE.match(rough[max(0, m.start() - 4): m.start()])
    }
    names = {th: reg[th] for th in found_thai if th in reg and reg[th]}
    passes.append({"pass": "pass2_names", "ok": True, "matched": len(names)})

    # Pass 3 — style in Ben's broadcast voice (radio adaptation)
    p3_prompt = _build_radio_pass3_prompt(rough, source, names, src_title)
    styled = await zai.zai_message(p3_prompt, max_tokens=1500, model=model, timeout=120.0)
    if not styled:
        raise HTTPException(502, "Pass 3 (style) returned empty text")
    passes.append({"pass": "pass3_style", "ok": True, "chars": len(styled)})

    # Pass 4 — strip fabricated Thai + mechanical checks
    cleaned = _strip_fabricated_thai(styled, source)
    mc = mech_checks(cleaned)
    fresh = freshness_scan(cleaned)
    passes.append({
        "pass": "pass4_cleanup",
        "ok": bool(mc["namecheck"]["ok"] and mc["stylecheck"]["ok"] and not fresh),
        "freshness_hits": len(fresh),
    })

    # Pass 5 — humanizer deep-edit
    humanized, h_meta = await _run_humanize(cleaned)
    passes.append({"pass": "pass5_humanize", **h_meta})

    # Pass 6 — figure check against source
    vf = await asyncio.to_thread(verify_figures, source, humanized)
    banner = verify_block(vf)
    passes.append({
        "pass": "pass6_figures",
        "ok": vf.get("ok", True),
        "checked": vf.get("checked", 0),
        "mismatches": len(vf.get("mismatches") or []),
    })

    # Explicit title / body split + title lint
    title, body, title_problems = split_title_body(humanized, fallback_title=src_title)
    if banner:
        body = f"{banner}\n\n{body}"

    entry = {
        "url": url,
        "source_title": src_title,
        "title": title,
        "body": body,
        "words": len(body.split()),
        "region": piece.get("region") or "GLOBAL",
        "source": piece.get("source") or "",
        "date": piece.get("date") or "",
        "title_problems": title_problems,
        "verify_block": bool(banner),
        "figure_check": vf,
        "freshness_hits": fresh,
        "passes": passes,
        "converted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    cache_put(entry)
    return entry


# ---------------------------------------------------------------- background job


_JOBS: dict[str, dict] = {}


async def _run_convert_job(job: dict, pieces: list[dict], model: str) -> None:
    for idx, piece in enumerate(pieces):
        row = job["pieces"][idx]
        url = str(piece.get("url") or "").strip()
        cached = cache_get(url)
        if cached:
            row.update({
                "state": "done",
                "cached": True,
                "title": cached["title"],
                "body": cached["body"],
                "words": cached.get("words", len(cached["body"].split())),
                "title_problems": cached.get("title_problems") or [],
                "verify_block": bool(cached.get("verify_block")),
                "passes": cached.get("passes") or [],
            })
            job["done"] += 1
            continue

        row["state"] = "converting"
        try:
            entry = await convert_piece(piece, model=model)
            row.update({
                "state": "done",
                "cached": False,
                "title": entry["title"],
                "body": entry["body"],
                "words": entry["words"],
                "title_problems": entry["title_problems"],
                "verify_block": entry["verify_block"],
                "passes": entry["passes"],
            })
            job["done"] += 1
        except HTTPException as e:
            row["state"] = "failed"
            row["error"] = str(e.detail)
            job["failed"] += 1
        except Exception as e:
            row["state"] = "failed"
            row["error"] = f"{type(e).__name__}: {e}"
            job["failed"] += 1

    job["status"] = "done"
    job["finished_at"] = time.time()


def start_convert(pieces: list[dict], model: str = CONVERT_MODEL) -> dict:
    """Launch a background convert job for ``pieces`` and return ``{convert_id, total}``."""
    cid = uuid.uuid4().hex[:10]
    job = {
        "convert_id": cid,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "total": len(pieces),
        "done": 0,
        "failed": 0,
        "pieces": [
            {
                "index": i,
                "url": p.get("url") or "",
                "source_title": (p.get("title") or "?")[:90],
                "region": p.get("region") or "GLOBAL",
                "state": "pending",
                "cached": False,
                "title": None,
                "body": None,
                "words": 0,
                "title_problems": [],
                "verify_block": False,
                "passes": [],
                "error": None,
            }
            for i, p in enumerate(pieces)
        ],
    }
    _JOBS[cid] = job

    def _runner():
        asyncio.run(_run_convert_job(job, pieces, model))

    threading.Thread(target=_runner, daemon=True).start()
    return {"convert_id": cid, "total": len(pieces)}


def convert_status(convert_id: str) -> dict | None:
    """Return the current status snapshot for ``convert_id``, or ``None``."""
    return _JOBS.get(convert_id)


# ---------------------------------------------------------------- placement + apply


async def _doc_slots(doc_id: str, kind: str, category: str) -> list[dict]:
    argv = radio_news.slots_argv(doc_id, kind, category)
    rc, out, _err = await radio_news._run(argv, timeout=30)
    if rc != 0:
        return []
    try:
        return json.loads(out).get("slots") or []
    except Exception:
        return []


async def _doc_text(doc_id: str) -> str | None:
    """Full plaintext across all tabs of ``doc_id`` (None if unreadable)."""
    argv = radio_news.text_argv(doc_id)
    rc, out, _err = await radio_news._run(argv, timeout=20)
    if rc != 0:
        return None
    try:
        return json.loads(out).get("text")
    except Exception:
        return None


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


async def plan_placement(
    doc_id: str,
    kind: str,
    category: str,
    urls: list[str],
    fallback_titles: dict[str, str] | None = None,
) -> dict:
    """Build the exact ``tab -> slot`` placement plan for ``urls`` in selection
    order.

    Uses the SAME empty-slot + doc-verified dedup rules that Apply executes,
    so the UI preview table and the real write cannot diverge.
    """
    slots = await _doc_slots(doc_id, kind, category)
    empty = [s for s in slots if not s.get("filled")]
    filled_count = sum(1 for s in slots if s.get("filled"))
    doc_txt = await _doc_text(doc_id)
    doc_norm = _norm_ws(doc_txt) if doc_txt is not None else None
    fb = fallback_titles or {}

    plan: list[dict] = []
    slot_idx = 0
    for i, url in enumerate(urls):
        conv = cache_get(url)
        title = conv["title"] if conv else (fb.get(url) or "")
        body = conv["body"] if conv else ""
        words = conv.get("words", len(body.split())) if conv else 0

        # Doc-verified dedup: only skip when the converted headline is
        # confirmed present in the live Google Doc text.
        t_norm = _norm_ws(title)
        if doc_norm is not None and len(t_norm) >= 12 and t_norm in doc_norm:
            plan.append({
                "index": i,
                "url": url,
                "title": title,
                "words": words,
                "converted": bool(conv),
                "action": "skip_in_doc",
                "tab": None,
                "slot": None,
                "reason": "already in doc (verified)",
            })
            continue

        if slot_idx >= len(empty):
            plan.append({
                "index": i,
                "url": url,
                "title": title,
                "words": words,
                "converted": bool(conv),
                "action": "no_slot",
                "tab": None,
                "slot": None,
                "reason": "no empty slots remaining",
            })
            continue

        target = empty[slot_idx]
        slot_idx += 1
        plan.append({
            "index": i,
            "url": url,
            "title": title,
            "body": body,
            "words": words,
            "converted": bool(conv),
            "title_problems": conv.get("title_problems") if conv else [],
            "verify_block": bool(conv.get("verify_block")) if conv else False,
            "action": "write",
            "tab": target["tab"],
            "slot": target["slot"],
            "reason": None,
        })

    return {
        "doc_id": doc_id,
        "kind": kind,
        "category": category,
        "slots_total": len(slots),
        "slots_filled": filled_count,
        "slots_empty": len(empty),
        "plan": plan,
    }


async def apply_converted(
    doc_id: str,
    kind: str,
    category: str,
    urls: list[str],
) -> dict:
    """Write pre-converted pieces into ``doc_id`` verbatim via ``fill --explicit``.

    Hard gate: every piece with ``action == 'write'`` MUST be in the convert
    cache — Apply never calls an LLM in this path.
    """
    missing = [u for u in urls if not cache_get(u)]
    if missing:
        raise HTTPException(
            400,
            f"{len(missing)} piece(s) not converted yet — run CONVERT SELECTED first",
        )

    placement = await plan_placement(doc_id, kind, category, urls)
    to_write = [p for p in placement["plan"] if p["action"] == "write"]
    skipped = [p for p in placement["plan"] if p["action"] != "write"]

    if not to_write:
        return {
            "written": [],
            "skipped": skipped,
            "placement": placement,
            "doc_check": None,
        }

    from app.media.radio_loop import _ledger_append

    argv = radio_news.fill_explicit_argv(doc_id, kind, category)
    written_all: list[dict] = []
    failed_all: list[dict] = []

    for p in to_write:
        payload = json.dumps({
            "pieces": [{
                "tab": p["tab"],
                "slot": p["slot"],
                "title": p["title"],
                "body": p["body"],
            }]
        }).encode()
        rc, out, err = await radio_news._run(argv, timeout=60, stdin=payload)
        if rc != 0:
            failed_all.append({
                "tab": p["tab"],
                "slot": p["slot"],
                "title": p["title"],
                "error": radio_news._fail(out, err),
            })
            continue
        data = json.loads(out)
        w = data.get("written") or []
        if w:
            written_all.extend(w)
            _ledger_append(doc_id, p["url"], p["title"], w[0])
        else:
            sk = (data.get("skipped") or [{}])[0]
            failed_all.append({
                "tab": p["tab"],
                "slot": p["slot"],
                "title": p["title"],
                "error": sk.get("reason") or "slot write skipped",
            })

    # Post-write read-only verification snapshot
    slots_after = await _doc_slots(doc_id, kind, category)
    doc_check = (
        {
            "total": len(slots_after),
            "filled": sum(1 for s in slots_after if s.get("filled")),
            "empty": [f"{s['tab']}{s['slot']}" for s in slots_after if not s.get("filled")],
            "slots": slots_after,
        }
        if slots_after
        else None
    )

    return {
        "written": written_all,
        "failed": failed_all,
        "skipped": skipped,
        "placement": placement,
        "doc_check": doc_check,
    }


# ---------------------------------------------------------------- routes


@router.post("/api/newsroom/radio/news/convert")
async def api_convert_start(body: dict = Body(...)):
    """Start a background Convert job for the ticked pieces."""
    pieces = body.get("pieces") or []
    if not isinstance(pieces, list) or not pieces:
        raise HTTPException(400, "pieces (non-empty list) is required")
    model = str(body.get("model") or CONVERT_MODEL)
    return start_convert(pieces, model=model)


@router.get("/api/newsroom/radio/news/convert/{convert_id}")
def api_convert_status(convert_id: str):
    """Poll a Convert job."""
    st = convert_status(convert_id)
    if not st:
        raise HTTPException(404, f"unknown convert_id: {convert_id}")
    return st


@router.post("/api/newsroom/radio/news/preview")
async def api_placement_preview(body: dict = Body(...)):
    """Dry-run placement plan against the live doc (preview == write contract)."""
    doc_id = body.get("doc_id")
    kind = body.get("kind")
    cat = body.get("category")
    urls = body.get("urls") or []
    titles = body.get("titles") or {}
    if not (doc_id and kind and cat):
        raise HTTPException(400, "doc_id, kind and category are required")
    return await plan_placement(str(doc_id), str(kind), str(cat), list(urls), dict(titles))
