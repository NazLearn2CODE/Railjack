#!/usr/bin/env python3
"""image_review_queue.py — build the human-reviewed repoint queue from Ahrefs CSVs.

READ-ONLY against WordPress (GET/HEAD only, paced). No writes, no deletes.

Input:  one or more Ahrefs site-audit CSV exports containing large-image URLs.
Output: image-review-queue.md (human) + .image_review_queue.json (ledger).

Classifies each crawled large image:
  in-content  -> repoint candidate: swap the <img src> in post content to an
                 existing smaller WP-generated variant (file untouched)
  featured    -> SKIP (theme serves small variants; storage bloat only)
  unused      -> note only (no action in this mode)

Usage:
  .venv/bin/python scripts/image_review_queue.py ~/Downloads/ahrefs_export.csv
"""
import csv
import json
import os
import re
import sys
import time
from urllib.parse import unquote

import httpx

SECRETS = os.path.expanduser("~/n8n/.secrets.env")
SITE = "www.thailandnow.in.th"
OUT_MD = "image-review-queue.md"
OUT_JSON = ".image_review_queue.json"
PACE = 0.6  # seconds between WP calls
EXT = r"(?:jpg|jpeg|png|webp|gif)"

VARIANT_RE = re.compile(r"-\d+x\d+(?=\.[a-zA-Z]+$)")


def load_secrets():
    env = {}
    with open(SECRETS) as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def client(env):
    return httpx.Client(
        base_url=env["WORDPRESS_URL"].rstrip("/") + "/wp-json/wp/v2",
        auth=(env["WORDPRESS_USERNAME"], env["WORDPRESS_APPLICATION_PASSWORD"]),
        timeout=45,
        headers={"User-Agent": "railjack-review-queue/1.0 (read-only audit)"},
        follow_redirects=True,
    )


def wp_get(c, path, params=None, note=None):
    """One paced GET. Retries 429/503 per Retry-After. Returns JSON or None."""
    for attempt in range(4):
        r = c.get(path, params=params or {})
        if r.status_code in (429, 503):
            wait = int(r.headers.get("Retry-After", "5") or 5)
            print(f"  .. {r.status_code} on {path}, backing off {wait}s", flush=True)
            time.sleep(wait)
            continue
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        time.sleep(PACE)
        return r.json() if r.content else None
    print(f"  !! giving up on {path} after retries", flush=True)
    return None


def wp_list_all(c, path, fields):
    """Page through a WP collection. Stops on EMPTY page only (paginator scar)."""
    out = []
    for page in range(1, 201):
        batch = wp_get(c, path, {"per_page": 100, "page": page, "_fields": fields})
        if batch is None:
            break
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            pass  # short page is NOT trustworthy as 'last' — next page decides
    return out


def urls_from_csv(path):
    """Scan every cell of the CSV for upload URLs; dedupe, keep order."""
    found = []
    seen = set()
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh):
            for cell in row:
                for m in re.finditer(
                    rf"https?://[^\"'\s,;]+/wp-content/uploads/[^\"'\s,;]+\.{EXT}", cell
                ):
                    u = m.group(0)
                    if u not in seen:
                        seen.add(u)
                        found.append(u)
    return found


def base_name(url):
    name = unquote(url.rsplit("/", 1)[-1])
    name = VARIANT_RE.sub("", name)
    name = re.sub(r"-scaled(?=\.[a-zA-Z]+$)", "", name)
    return name.lower()


def size_of(url):
    m = VARIANT_RE.search(url)
    if m:
        w, h = m.group(0).lstrip("-").split("x")
        return int(w), int(h)
    return None


def pick_variant(media):
    """Best existing generated variant to repoint to: smallest bytes among
    width>=1000; never 'full'. Returns (url, filesize, w, h) or None."""
    det = media.get("media_details") or {}
    sizes = det.get("sizes") or {}
    cands = []
    for name, s in sizes.items():
        if name == "full":
            continue
        if (s.get("width") or 0) >= 1000:
            cands.append((s.get("filesize") or 1 << 30, s.get("file"), s))
    if not cands:
        return None
    cands.sort()
    s = cands[0][2]
    base = (det.get("file") or "").rsplit("/", 1)[0]
    url = f"https://{SITE}/wp-content/uploads/{base}/{s['file']}" if base else None
    return (url, s.get("filesize"), s.get("width"), s.get("height"))


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: image_review_queue.py CSV [CSV...] [--fetch-pages]")
    csvs = [a for a in sys.argv[1:] if not a.startswith("--")]
    env = load_secrets()
    c = client(env)

    print("== 1. URLs from CSV ==")
    raw_urls = []
    for p in csvs:
        got = urls_from_csv(p)
        print(f"  {p}: {len(got)} upload URLs")
        raw_urls.extend(got)
    # dedupe by canonical base+ext — keep the largest-width variant seen
    by_base = {}
    for u in raw_urls:
        b = base_name(u)
        cur = by_base.get(b)
        if cur is None or (size_of(u) or (0,)) > (size_of(cur) or (0,)):
            by_base[b] = u
    print(f"  unique images: {len(by_base)}")

    print("== 2. media library (read-only, paginated) ==")
    media_all = wp_list_all(c, "/media", "id,source_url,media_details")
    print(f"  {len(media_all)} attachments")
    media_by_base = {}
    for m in media_all:
        media_by_base.setdefault(base_name(m.get("source_url", "")), m)

    print("== 3. content scan targets ==")
    types = wp_get(c, "/types") or {}
    cpts = [k for k, v in types.items() if (v or {}).get("rest_base") in
            ("posts", "pages", "events") or k in ("post", "page", "event")]
    posts = []
    for k in cpts:
        rb = (types.get(k) or {}).get("rest_base")
        if not rb:
            continue
        got = wp_list_all(c, f"/{rb}", "id,link,title,content,featured_media")
        print(f"  /{rb}: {len(got)}")
        posts.extend(got)

    print("== 4. classify ==")
    queue, skips, unused = [], [], []
    for base, url in sorted(by_base.items()):
        m = media_by_base.get(base)
        det = (m or {}).get("media_details") or {}
        orig_bytes = det.get("filesize")
        if orig_bytes is None:
            try:
                h = c.head(url)
                orig_bytes = int(h.headers.get("content-length", 0) or 0)
                time.sleep(PACE)
            except Exception:
                orig_bytes = 0
        # find embedding posts
        embeds, featured_in = [], []
        for p in posts:
            content = ((p.get("content") or {}).get("rendered") or "")
            if base in content.lower():
                embeds.append(p)
            if m and p.get("featured_media") == m.get("id"):
                featured_in.append(p)
        mb = (orig_bytes or 0) / 1e6
        item = {
            "image": url, "base": base, "attachment_id": (m or {}).get("id"),
            "size_mb": round(mb, 2), "width": det.get("width"), "height": det.get("height"),
            "embeds": [{"id": p["id"], "link": p["link"], "title": (p.get("title") or {}).get("rendered", "")} for p in embeds],
            "featured_in": [{"id": p["id"], "link": p["link"]} for p in featured_in],
        }
        if embeds:
            v = pick_variant(m or {})
            item["variant"] = {"url": v[0], "size_mb": round((v[1] or 0) / 1e6, 2),
                               "width": v[2], "height": v[3]} if v else None
            item["action"] = "repoint" if v else "NEEDS-SHRINK (no suitable variant)"
            item["status"] = "pending"
            queue.append(item)
        elif featured_in:
            item["action"] = "SKIP featured-only (theme serves variants)"
            item["status"] = "skip-featured"
            skips.append(item)
        else:
            item["action"] = "NOTE unused (no action)"
            item["status"] = "unused"
            unused.append(item)

    queue.sort(key=lambda i: -i["size_mb"])
    ledger = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source_csvs": csvs,
              "queue": queue, "skip_featured": skips, "unused": unused}
    with open(OUT_JSON, "w") as fh:
        json.dump(ledger, fh, indent=1)

    with open(OUT_MD, "w") as fh:
        fh.write(f"# Image review queue ({len(queue)} repoint candidates)\n\n")
        fh.write(f"Source: {', '.join(csvs)} · generated {ledger['generated_at']}\n")
        fh.write("Mode: repoint-only. No file is replaced or deleted. Featured-only images skipped.\n\n")
        for n, i in enumerate(queue, 1):
            fh.write(f"## {n}. {i['base']} — {i['size_mb']} MB\n")
            fh.write(f"- image: {i['image']}\n")
            fh.write(f"- dims: {i['width']}x{i['height']} · attachment {i['attachment_id']}\n")
            for e in i["embeds"]:
                fh.write(f"- used on: [{e['title'] or e['link']}]({e['link']})\n")
            if i.get("variant"):
                v = i["variant"]
                fh.write(f"- repoint to: {v['url']} ({v['size_mb']} MB, {v['width']}x{v['height']})\n")
                fh.write(f"- saves: ~{round(i['size_mb'] - v['size_mb'], 2)} MB/page-load\n")
            else:
                fh.write("- NO suitable variant — needs a shrunk copy (manual decision)\n")
            fh.write("\n")
        fh.write(f"\n## Skipped featured-only: {len(skips)}\n")
        for i in skips:
            fh.write(f"- {i['base']} ({i['size_mb']} MB) — featured in {len(i['featured_in'])} page(s)\n")
        fh.write(f"\n## Unused (no action): {len(unused)}\n")
        for i in unused:
            fh.write(f"- {i['base']} ({i['size_mb']} MB)\n")

    print(f"\nwrote {OUT_MD} + {OUT_JSON}")
    print(f"repoint candidates: {len(queue)} · featured-skips: {len(skips)} · unused: {len(unused)}")


if __name__ == "__main__":
    main()
