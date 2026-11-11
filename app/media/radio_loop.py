"""RADIO ▸ News Fill — unified per-piece streaming pipeline.

Pipeline (Naz 2026-11-10):
  1. Plan placement ONCE against the live Google Doc (empty slots in
     broadcast order + doc-verified dedup on already-known titles).
  2. For each piece with ``action == 'write'`` (sequentially, in tick order):
     a. ``rewritten: true`` (IDE SCOUT pre-wrote + humanized in Editor Ben's
        radio shape): skip ``cache_get`` and ``convert_piece`` (0 hub LLM
        calls), run advisory 120s ``verify_figures`` + ``freshness_scan``,
        and write verbatim into the assigned ``(tab, slot)``.
     b. Raw piece: check durable convert cache; on miss run
        ``radio_convert.convert_piece`` with ``PIECE_TIMEOUT`` (300s) + 1 retry.
     c. On double convert failure/timeout: write fail-safe
        ``[CONVERT FAILED — <url>]`` INTO the assigned slot (never ledgered,
        so a re-run can replace it) and continue to the next piece.
     d. Re-verify converted headline against the live doc text (doc-verified
        dedup for freshly generated headlines).
     e. Write verbatim into ``(tab, slot)`` via ``radio_news.py fill --explicit``
        (2 attempts) and record in ``~/.config/railjack/radio_written.jsonl``.
  3. Run read-only ``slots`` verification snapshot (``doc_check``) and
     idempotent ``doc_format.py`` publication polish (``format``, 420s timeout).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from app.media import radio_convert, radio_news
from app.media.radio_convert import freshness_scan, verify_figures

router = APIRouter()

LOOP_MODEL = "dsh"
PIECE_TIMEOUT = 300.0

_JOBS: dict[str, dict] = {}
_LEDGER = Path.home() / ".config" / "railjack" / "radio_written.jsonl"


# ---------------------------------------------------------------- ledger


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "")).strip().lower()


def _ledger_load(doc_id: str) -> tuple[set[str], set[str]]:
    """Return ``(urls, normalized_titles)`` recorded in the ledger for ``doc_id``."""
    urls: set[str] = set()
    titles: set[str] = set()
    if not _LEDGER.exists():
        return urls, titles
    for line in _LEDGER.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("doc_id") == doc_id:
            if r.get("url"):
                urls.add(r["url"])
            if r.get("title"):
                titles.add(_norm_title(r["title"]))
    return urls, titles


def _ledger_append(doc_id: str, url: str, title: str, slot_info: dict) -> None:
    """Append a written-slot record to ``_LEDGER``."""
    _LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "doc_id": doc_id,
        "url": url,
        "title": title,
        "tab": slot_info.get("tab"),
        "slot": slot_info.get("slot"),
    }
    with _LEDGER.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def _filter_written(doc_id: str, pieces: list[dict]) -> list[tuple[str, str | None]]:
    """Never pre-skip from the ledger alone — dedup is doc-verified against the
    live Google Doc text inside ``plan_placement`` and ``_run_loop``."""
    return [("pending", None) for _ in pieces]


# ---------------------------------------------------------------- explicit write


async def _write_explicit(
    doc_id: str,
    kind: str,
    category: str,
    tab: str,
    slot: int,
    title: str,
    body: str,
) -> tuple[dict | None, str | None]:
    """Write ``(title, body)`` verbatim into ``(tab, slot)`` via ``fill --explicit``.

    Retries once on transient Docs API / timeout error. Catches ``Exception``
    (including ``HTTPException(504)``) per attempt so attempt 2 always runs.
    """
    argv = radio_news.fill_explicit_argv(doc_id, kind, category)
    payload = json.dumps({
        "pieces": [{"tab": tab, "slot": slot, "title": title, "body": body}]
    }).encode()
    last_err = "unknown error"
    for attempt in (1, 2):
        try:
            rc, out, err = await radio_news._run(argv, timeout=90, stdin=payload)
        except Exception as e:
            rc, out, err = 1, b"", str(e).encode()
        if rc == 0:
            try:
                data = json.loads(out)
            except Exception as e:
                return None, f"unparseable fill stdout: {e}"
            written = data.get("written") or []
            if written:
                return written[0], None
            skipped = (data.get("skipped") or [{}])[0]
            return None, skipped.get("reason") or "slot write skipped by radio_news"
        last_err = radio_news._fail(out, err)
        if attempt == 1:
            await asyncio.sleep(2.0)
    return None, last_err


# ---------------------------------------------------------------- streaming loop


async def _run_loop(
    job: dict,
    doc_id: str,
    kind: str,
    category: str,
    pieces: list[dict],
    model: str,
) -> None:
    urls = [str(p.get("url") or "").strip() for p in pieces]
    fb_titles = {
        str(p.get("url") or "").strip(): str(p.get("title") or "")
        for p in pieces
    }
    placement = await radio_convert.plan_placement(doc_id, kind, category, urls, fb_titles)
    doc_txt = await radio_convert._doc_text(doc_id)
    doc_norm = radio_convert._norm_ws(doc_txt) if doc_txt is not None else None

    for idx, plan_row in enumerate(placement["plan"]):
        row = job["pieces"][idx]
        piece = pieces[idx]
        url = plan_row["url"]
        action = plan_row["action"]

        if action != "write":
            row["state"] = "skipped"
            row["error"] = plan_row.get("reason") or action
            job["skipped"] += 1
            continue

        tab, slot = plan_row["tab"], plan_row["slot"]

        # 1a. Pre-rewritten by IDE SCOUT (`rewritten: true`): skip hub LLM
        #     passes and convert cache; split + advisory check + write verbatim.
        if piece.get("rewritten"):
            raw_title = str(piece.get("title") or "").strip()
            raw_body = str(piece.get("content") or piece.get("text") or "").strip()
            title, body, title_problems = radio_convert.split_title_body(
                f"{raw_title}\n\n{raw_body}" if raw_title else raw_body,
                fallback_title=raw_title,
            )
            src = str(piece.get("source_text") or "").strip()
            row["state"] = "verifying"
            try:
                vf = await asyncio.wait_for(
                    asyncio.to_thread(verify_figures, src or raw_body, body),
                    timeout=120.0,
                )
            except Exception as e:
                vf = {"ok": True, "skipped": f"verify timeout/error: {e}", "mismatches": []}
            fresh = freshness_scan(body)
            row["title"] = title
            row["words"] = len(body.split())
            row["passes"] = [
                {"pass": "ide_rewritten", "ok": True},
                {"pass": "figures", "ok": vf.get("ok", True), "checked": vf.get("checked", 0)},
            ]
            row["verify"] = {
                "figures": vf,
                "freshness_hits": fresh,
                "title_problems": title_problems,
                "verify_block": False,
            }
            conv = {
                "url": url,
                "title": title,
                "body": body,
                "words": len(body.split()),
                "title_problems": title_problems,
                "verify_block": False,
            }
        else:
            # 1b. Raw piece: check durable convert cache, else convert on the fly
            conv = radio_convert.cache_get(url)
            if conv:
                row["passes"] = [{"pass": "cache_hit", "ok": True}]
            else:
                row["state"] = "rewriting"
                last_err = None
                for attempt in (1, 2):
                    try:
                        conv = await asyncio.wait_for(
                            radio_convert.convert_piece(piece, model=model),
                            timeout=PIECE_TIMEOUT,
                        )
                        break
                    except asyncio.TimeoutError:
                        last_err = f"convert timed out after {int(PIECE_TIMEOUT)}s (attempt {attempt})"
                    except HTTPException as e:
                        last_err = f"{e.detail} (attempt {attempt})"
                    except Exception as e:
                        last_err = f"{type(e).__name__}: {e} (attempt {attempt})"

                if not conv:
                    # Fail-safe placeholder into the assigned slot; NEVER ledger
                    # so a re-run can replace it.
                    fs_title = (piece.get("title") or "Untitled")[:90]
                    fs_body = (
                        f"[CONVERT FAILED — {url or 'no url'}]\n"
                        f"Reason: {last_err}\n"
                        f"Source headline: {piece.get('title') or '?'}"
                    )
                    row["state"] = "writing"
                    slot_info, werr = await _write_explicit(
                        doc_id, kind, category, tab, slot, fs_title, fs_body
                    )
                    row["state"] = "failsafe" if slot_info else "failed"
                    row["slot"] = slot_info
                    row["error"] = last_err if not werr else f"{last_err}; failsafe write: {werr}"
                    job["failed"] += 1
                    continue

            row["title"] = conv["title"]
            row["words"] = conv.get("words", 0)
            row["passes"] = conv.get("passes") or []
            row["verify"] = {
                "figures": conv.get("figure_check") or {"ok": True},
                "freshness_hits": conv.get("freshness_hits") or [],
                "title_problems": conv.get("title_problems") or [],
                "verify_block": bool(conv.get("verify_block")),
            }

        # 2. Post-convert doc-verified dedup (when headline changed during convert)
        t_norm = radio_convert._norm_ws(conv["title"])
        if doc_norm is not None and len(t_norm) >= 12 and t_norm in doc_norm:
            row["state"] = "skipped"
            row["error"] = "already in doc (verified)"
            job["skipped"] += 1
            continue

        # 3. Verbatim explicit-slot write
        row["state"] = "writing"
        slot_info, err = await _write_explicit(
            doc_id, kind, category, tab, slot, conv["title"], conv["body"]
        )
        if err:
            row["state"] = "failed"
            row["error"] = f"write {tab}{slot}: {err}"
            job["failed"] += 1
            continue

        row["slot"] = slot_info
        row["state"] = "done"
        job["done"] += 1
        _ledger_append(doc_id, url, conv["title"], slot_info or {"tab": tab, "slot": slot})
        if doc_norm is not None:
            doc_norm += " " + t_norm

    # Post-loop read-only slot occupancy verification
    try:
        rc, out, _err = await radio_news._run(
            radio_news.slots_argv(doc_id, kind, category), timeout=30
        )
        if rc == 0:
            slots = json.loads(out).get("slots") or []
            job["doc_check"] = {
                "total": len(slots),
                "filled": sum(1 for s in slots if s.get("filled")),
                "empty": [f"{s['tab']}{s['slot']}" for s in slots if not s.get("filled")],
                "slots": slots,
            }
    except Exception:
        pass

    # Server-side FORMAT at loop end (idempotent, failure-isolated, 420s timeout)
    if job["done"] > 0:
        try:
            rc, out, err = await radio_news._run(
                [radio_news.PY, str(radio_news.DFORMAT), "--doc", str(doc_id)],
                timeout=420,
            )
            job["format"] = (
                json.loads(out) if rc == 0 else {"ok": False, "error": radio_news._fail(out, err)}
            )
        except Exception as e:
            job["format"] = {"ok": False, "error": str(e)}

    job["status"] = "done"
    job["finished_at"] = time.time()


def start_loop(
    doc_id: str,
    kind: str,
    category: str,
    pieces: list[dict],
    model: str = LOOP_MODEL,
) -> dict:
    """Start a background streaming Apply loop and return ``{loop_id, total}``."""
    loop_id = uuid.uuid4().hex[:10]
    job = {
        "loop_id": loop_id,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "doc_id": doc_id,
        "kind": kind,
        "category": category,
        "model": model,
        "total": len(pieces),
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "doc_check": None,
        "format": None,
        "pieces": [
            {
                "index": i,
                "title": (p.get("title") or "?")[:90],
                "region": p.get("region") or "GLOBAL",
                "state": "pending",
                "passes": [],
                "verify": None,
                "slot": None,
                "words": 0,
                "error": None,
            }
            for i, p in enumerate(pieces)
        ],
    }
    _JOBS[loop_id] = job

    def _runner():
        asyncio.run(_run_loop(job, doc_id, kind, category, pieces, model))

    threading.Thread(target=_runner, daemon=True).start()
    return {"loop_id": loop_id, "total": len(pieces)}


def loop_status(loop_id: str) -> dict | None:
    """Return the current poll snapshot for ``loop_id``, or ``None``."""
    j = _JOBS.get(loop_id)
    if not j:
        return None
    return {
        "loop_id": j["loop_id"],
        "status": j["status"],
        "total": j["total"],
        "done": j["done"],
        "failed": j["failed"],
        "skipped": j.get("skipped", 0),
        "elapsed_s": round((j["finished_at"] or time.time()) - j["started_at"], 1),
        "doc_check": j.get("doc_check"),
        "format": j.get("format"),
        "pieces": j["pieces"],
    }


# ---------------------------------------------------------------- routes


@router.post("/api/newsroom/radio/news/run")
async def api_run_start(body: dict = Body(...)):
    """Start the sequential per-piece radio streaming loop."""
    doc_id = body.get("doc_id")
    kind = body.get("kind")
    cat = body.get("category")
    pieces = body.get("pieces") or []
    model = str(body.get("model") or LOOP_MODEL)
    if not (doc_id and kind and cat):
        raise HTTPException(400, "doc_id, kind and category are required")
    if not isinstance(pieces, list) or not pieces:
        raise HTTPException(400, "pieces (non-empty list) is required")
    return start_loop(str(doc_id), str(kind), str(cat), pieces, model=model)


@router.get("/api/newsroom/radio/news/run/{loop_id}")
def api_run_status(loop_id: str):
    """Poll a running or finished radio streaming loop."""
    st = loop_status(loop_id)
    if not st:
        raise HTTPException(404, f"unknown loop_id: {loop_id}")
    return st
