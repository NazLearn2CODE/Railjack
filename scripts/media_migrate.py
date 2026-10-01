#!/usr/bin/env python3
"""Migrate oversized media attachments to ≤1 MB JPEG copies (Ben's 1 MB rule).

For every image attachment over ``--limit-mb``:
  1. download the original                → archived to ARCHIVE_DIR (undo button)
  2. recompress with Pillow               → JPEG, longest side ≤ --max-side,
                                            quality stepped down until ≤ limit
  3. upload as a NEW attachment           → title/alt/caption copied from the old
  4. repoint every reference              → content srcs (incl. old size variants)
                                            + featured_media on any record
  5. verify nothing references the old file → archive + DELETE the old attachment
                                            (WP media cannot trash)

State/checkpoint file (.media_migrate_state.json) makes the run resumable:
already-migrated ids are skipped on re-run. Paced: ``--delay`` seconds between
attachments (default 1.0) — the site's firewall rate-limits bursts.

Usage:
  python3 scripts/media_migrate.py --dry-run     # plan only, no writes
  python3 scripts/media_migrate.py               # live run
  python3 scripts/media_migrate.py --limit-mb 1 --max-side 2048 --delay 1.0
"""

import argparse
import asyncio
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import thailandnow as tn  # noqa: E402
from fastapi import HTTPException  # noqa: E402

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / ".media-archive-migrate"
STATE_FILE = Path(__file__).resolve().parent.parent / ".media_migrate_state.json"
REPORT_FILE = Path(__file__).resolve().parent.parent / ".media_migrate_report.json"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text())
    return {"migrated": {}, "skipped": {}, "deleted": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1))


def compress_to_limit(data: bytes, limit: int, max_side: int) -> tuple[bytes, str]:
    """JPEG-flatten + resize + quality steps until ≤ limit. Returns (bytes, label)."""
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.split()[-1])
        im = base
    else:
        im = im.convert("RGB")

    def resized(img: "Image.Image", side: int) -> "Image.Image":
        w, h = img.size
        scale = max(1.0, max(w, h) / side)
        if scale <= 1.0:
            return img
        return img.resize((max(1, int(w / scale)), max(1, int(h / scale))), Image.LANCZOS)

    attempts = [(max_side, 85), (max_side, 78), (max_side, 70),
                (1600, 80), (1600, 70), (1280, 78), (1280, 68), (1024, 75)]
    best = None
    for side, q in attempts:
        im2 = resized(im, side)
        buf = io.BytesIO()
        im2.save(buf, "JPEG", quality=q, optimize=True)
        best = (buf.getvalue(), f"jpeg {side}px q{q}")
        if len(buf.getvalue()) <= limit:
            return buf.getvalue(), f"jpeg {side}px q{q}"
    return best


async def page_all(ep: str, fields: str, pause: float = 0.2) -> list[dict]:
    out, page = [], 1
    while page <= 200:
        try:
            batch = await tn._wp("GET", ep, {"per_page": 100, "page": page, "_fields": fields})
        except HTTPException as e:
            if "invalid_page" in str(e):
                break
            raise
        if not batch:
            break
        out.extend(batch)
        page += 1
        await asyncio.sleep(pause)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-mb", type=float, default=1.0)
    ap.add_argument("--max-side", type=int, default=2048)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-items", type=int, default=0, help="0 = no cap (testing)")
    args = ap.parse_args()
    limit = int(args.limit_mb * 1_000_000)

    state = load_state()
    ARCHIVE_DIR.mkdir(exist_ok=True)

    log("fetching media library + content…")
    media = await page_all("/media", "id,source_url,mime_type,media_details,title,caption")
    big = []
    meta_by_id = {}
    for m in media or []:
        if not str(m.get("mime_type") or "").startswith("image/"):
            continue
        d = m.get("media_details") or {}
        try:
            size = int(d.get("filesize") or 0)
        except (TypeError, ValueError):
            continue
        if size > limit:
            big.append({"id": m["id"], "u": m["source_url"], "size": size})
            meta_by_id[m["id"]] = m
    log(f"library: {len(media or [])} items | oversized: {len(big)} "
        f"| {sum(b['size'] for b in big) / 1048576:.0f} MB")

    log("fetching content records…")
    recs = []
    for ep in ("/posts", "/pages", "/event"):
        recs.extend(await page_all(ep, "id,link,title,content,featured_media"))
    log(f"records: {len(recs)}")

    # usage maps: old url (any size of that attachment) → records referencing it
    url_owner = {}   # old attachment id -> {url: True} for source_url + all size urls
    for m in media or []:
        aid = m.get("id")
        d = m.get("media_details") or {}
        urls = {m.get("source_url")}
        for s in (d.get("sizes") or {}).values():
            if isinstance(s, dict) and s.get("source_url"):
                urls.add(s["source_url"])
        url_owner[aid] = {u for u in urls if u}

    refs = {}    # attachment id -> list of record ids referencing it (content)
    featured = {}  # attachment id -> [record ids]
    for r in recs:
        html = (r.get("content") or {}).get("rendered", "") or ""
        for aid, urls in url_owner.items():
            if any(u and u in html for u in urls):
                refs.setdefault(aid, []).append(r["id"])
        if r.get("featured_media"):
            featured.setdefault(r["featured_media"], []).append(r["id"])

    plan = []
    for b in big:
        aid = b["id"]
        if str(aid) in state["migrated"] or aid in state.get("deleted", []):
            continue
        plan.append({"id": aid, "size": b["size"],
                     "klass": ("full-used" if aid in refs else
                               "featured" if aid in featured else
                               "variant-used" if any(
                                   any(u and u in ((r.get("content") or {}).get("rendered") or "")
                                       for u in url_owner.get(aid, set()))
                                   for r in recs) else "unused"),
                     "records": sorted(set(refs.get(aid, []) + featured.get(aid, []))),
                     "featured_records": featured.get(aid, [])})
    plan.sort(key=lambda x: -x["size"])
    log(f"plan: {len(plan)} attachments to migrate "
        f"({sum(1 for p in plan if p['klass'] == 'full-used')} full-used, "
        f"{sum(1 for p in plan if p['klass'] == 'featured')} featured, "
        f"{sum(1 for p in plan if p['klass'] == 'variant-used')} variant-used, "
        f"{sum(1 for p in plan if p['klass'] == 'unused')} unused)")

    if args.dry_run:
        json.dump(plan, open(REPORT_FILE.with_name(".media_migrate_plan.json"), "w"), default=str)
        log("dry-run — plan saved to .media_migrate_plan.json")
        return

    if args.max_items:
        plan = plan[: args.max_items]

    results = []
    from PIL import Image  # noqa: F401 — fail fast before the loop
    import httpx

    url, user, pwd = tn._wp_creds()
    auth = (user, pwd)

    for n, item in enumerate(plan, 1):
        aid, size = item["id"], item["size"]
        key = str(aid)
        log(f"[{n}/{len(plan)}] {item['klass']} #{aid} ({size / 1048576:.1f} MB)")
        try:
            old_meta = meta_by_id.get(aid) or await tn._wp("GET", f"/media/{aid}")
            src = old_meta.get("source_url")
            async with httpx.AsyncClient(timeout=120, follow_redirects=True, auth=auth) as c:
                r = await c.get(src)
                r.raise_for_status()
                original = r.content
            (ARCHIVE_DIR / f"{aid}-{src.split('/')[-1]}").write_bytes(original)

            data, label = compress_to_limit(original, limit, args.max_side)
            stem = src.split("/")[-1].rsplit(".", 1)[0][:80]
            new_name = f"{stem}-1mb.jpg"
            async with httpx.AsyncClient(timeout=120, auth=auth) as c:
                up = await c.post(
                    f"{url}/wp-json/wp/v2/media",
                    files={"file": (new_name, data, "image/jpeg")},
                    params={"title": (old_meta.get("title") or {}).get("rendered", "") or new_name},
                )
            if up.status_code >= 400:
                raise RuntimeError(f"upload {up.status_code}: {up.text[:120]}")
            new_att = up.json()
            new_id, new_url = new_att.get("id"), new_att.get("source_url")
            # copy alt text + caption onto the new attachment
            await tn._wp("POST", f"/media/{new_id}", json_body={
                "alt_text": old_meta.get("alt_text") or "",
                "caption": (old_meta.get("caption") or {}).get("raw", "") or "",
            })
            log(f"  uploaded #{new_id} ({label}, {len(data) / 1024:.0f} KB) → {new_url.split('/')[-1]}")

            # swap references: content urls (any size variant) + featured ids
            old_urls = sorted(url_owner.get(aid, set()), key=len, reverse=True)
            swap = {"records": 0, "featured": 0}
            for rid in item["records"]:
                rec = next((x for x in recs if x["id"] == rid), None)
                if rec is None:
                    continue
                html = (rec.get("content") or {}).get("rendered", "") or ""
                if not any(u and u in html for u in old_urls):
                    continue
                new_html = html
                for u in old_urls:
                    new_html = new_html.replace(u, new_url)
                rb = await tn._wp_resolve_rest_base(rid)
                await tn._wp("POST", f"/{rb}/{rid}", json_body={"content": new_html})
                swap["records"] += 1
                await asyncio.sleep(args.delay)
            for rid in item["featured_records"]:
                rec = next((x for x in recs if x["id"] == rid), None)
                rb = rec and await tn._wp_resolve_rest_base(rid)
                if rb:
                    await tn._wp("POST", f"/{rb}/{rid}", json_body={"featured_media": new_id})
                    swap["featured"] += 1
                    await asyncio.sleep(args.delay)

            # verify nothing references the old urls anymore
            leftover = 0
            for rid in swap and item["records"] or []:
                rec = next((x for x in recs if x["id"] == rid), None)
                if rec is None:
                    continue
                html = (rec.get("content") or {}).get("rendered", "") or ""
                leftover += sum(1 for u in old_urls if u and u in html)

            if leftover == 0:
                await tn._wp("DELETE", f"/media/{aid}", params={"force": "true"})
                state.setdefault("deleted", []).append(aid)
                log(f"  ✓ swapped {swap['records']} records + {swap['featured']} featured; old #{aid} deleted")
            else:
                # keep old attachment — references remain, never break a page
                log(f"  ! {leftover} reference(s) still point at old file — old attachment KEPT")
                state["skipped"][key] = f"{leftover} leftover references"

            state["migrated"][key] = {
                "new_id": new_id, "new_url": new_url, "label": label,
                "old_size_mb": round(size / 1048576, 1), "klass": item["klass"],
                "swap": swap, "deleted": leftover == 0, "at": datetime.now().isoformat(timespec="seconds"),
            }
            results.append({"id": aid, "new_id": new_id, "klass": item["klass"],
                            "old_mb": round(size / 1048576, 1), "new_kb": len(data) // 1024,
                            "swap": swap, "deleted": leftover == 0})
        except Exception as e:  # noqa: BLE001 — one attachment never kills the run
            log(f"  ✗ FAILED: {type(e).__name__}: {str(e)[:160]}")
            state["skipped"][key] = str(e)[:200]
            results.append({"id": aid, "ok": False, "error": str(e)[:200], "klass": item["klass"]})
        save_state(state)
        if n < len(plan):
            await asyncio.sleep(args.delay)

    save_state(state)
    migrated = [r for r in results if r.get("new_id")]
    before = sum(p["size"] for p in plan) / 1048576
    after = sum(r.get("new_kb", 0) for r in migrated) / 1024
    summary = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "planned": len(plan), "migrated": len(migrated),
        "failed": len(results) - len(migrated),
        "library_weight_before_mb": round(before, 1),
        "library_weight_after_mb": round(after, 1),
        "deleted_old": len(state.get("deleted", [])),
        "results": results,
    }
    REPORT_FILE.write_text(json.dumps(summary, indent=1))
    log(f"DONE: migrated {summary['migrated']}/{summary['planned']} | failed {summary['failed']} "
        f"| {summary['library_weight_before_mb']} MB → {summary['library_weight_after_mb']} MB "
        f"| old deleted {summary['deleted_old']} | report: {REPORT_FILE.name}")


if __name__ == "__main__":
    asyncio.run(main())
