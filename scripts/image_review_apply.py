#!/usr/bin/env python3
"""image_review_apply.py — shrink-copy + repoint, per reviewed page.

SAFE MODE: uploads a compressed JPEG copy of each reviewed image, repoints ONE
page's content to the copies in a single write, keeps a raw-content snapshot
for one-write revert. NEVER deletes anything.

Modes:
  plan   --page POSTID        show exactly what would change (default, no writes)
  apply  --page POSTID        upload copies + one content write for that page
  revert --page POSTID        restore the page from its snapshot (one write)

Reads .image_review_queue.json (from image_review_queue.py) for the item list.
"""
import io
import json
import os
import re
import sys
import time

import httpx
from PIL import Image

from image_review_queue import wp_list_all

SECRETS = os.path.expanduser("~/n8n/.secrets.env")
LEDGER = ".image_review_queue.json"
BACKUPS = ".image_review_backups"
PACE = 0.6
MAX_BYTES = 1_000_000
QUALITY_LADDER = [85, 80, 75, 70]


def load_secrets():
    env = {}
    with open(SECRETS) as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def client(env, authed=True):
    return httpx.Client(
        base_url=env["WORDPRESS_URL"].rstrip("/") + "/wp-json/wp/v2",
        auth=(env["WORDPRESS_USERNAME"], env["WORDPRESS_APPLICATION_PASSWORD"]) if authed else None,
        timeout=90,
        headers={"User-Agent": "railjack-image-review/1.0"},
        follow_redirects=True,
    )


def wp_get(c, path, params=None):
    for _ in range(4):
        r = c.get(path, params=params or {})
        if r.status_code in (429, 503):
            time.sleep(int(r.headers.get("Retry-After", "5") or 5))
            continue
        r.raise_for_status()
        time.sleep(PACE)
        return r.json() if r.content else None
    raise RuntimeError(f"retries exhausted: GET {path}")


def to_jpeg(data: bytes, start_q: int = 85) -> tuple[bytes, int]:
    """PNG bytes -> JPEG bytes ≤1MB, same dimensions, quality stepped down."""
    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        img_rgba = img.convert("RGBA")
        bg.paste(img_rgba, mask=img_rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    ladder = [start_q] + [q for q in QUALITY_LADDER if q < start_q]
    for q in ladder:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        if buf.tell() <= MAX_BYTES:
            return buf.getvalue(), q
    return buf.getvalue(), ladder[-1]


def stem_of(base: str) -> str:
    """'thai-jackfruit-curry.png' -> 'thai-jackfruit-curry' (ledger base keeps ext)."""
    return re.sub(r"\.[a-zA-Z]+$", "", base)


def old_url_pattern(base: str):
    """URLs on the site ending in the base filename, with or without -WxH, .png."""
    esc = re.escape(stem_of(base))
    return re.compile(rf"https?://[^\s\"'<>)]*{esc}(?:-\d+x\d+)?\.png", re.I)


def get_post_raw(c, post_id, rest_base):
    p = wp_get(c, f"/{rest_base}/{post_id}", {"context": "edit"})
    return p, (p.get("content") or {}).get("raw", "")


def find_items(ledger, post_id):
    out = []
    for i in ledger["queue"]:
        if i.get("status") != "pending":
            continue
        if any(e["id"] == post_id for e in i.get("embeds", [])):
            out.append(i)
    return out


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "plan"
    if "--page" not in args:
        sys.exit("usage: image_review_apply.py plan|apply|revert --page POSTID")
    post_id = int(args[args.index("--page") + 1])
    ledger = json.load(open(LEDGER))

    # which rest_base hosts this post?
    env = load_secrets()
    c = client(env)
    rest_base = None
    for rb in ("posts", "pages", "event"):
        try:
            p = wp_get(c, f"/{rb}/{post_id}", {"context": "edit"})
            if p and p.get("id") == post_id:
                rest_base = rb
                break
        except Exception:
            continue
    if not rest_base:
        sys.exit(f"post {post_id} not found in posts/pages/event")

    post, raw = get_post_raw(c, post_id, rest_base)
    link = post.get("link", "")
    print(f"target: [{link}] ({rest_base}/{post_id})")

    if mode == "revert":
        snap = os.path.join(BACKUPS, f"post-{post_id}.before.html")
        if not os.path.exists(snap):
            sys.exit(f"no snapshot at {snap}")
        new_raw = open(snap, encoding="utf-8").read()
        r = c.put(f"/{rest_base}/{post_id}", json={"content": new_raw})
        r.raise_for_status()
        print(f"REVERTED post {post_id} from snapshot ({len(new_raw)} bytes). Done.")
        return

    items = find_items(ledger, post_id)
    if not items:
        sys.exit("no pending queue items for this page")
    print(f"{len(items)} pending image(s) on this page:")

    plan = []
    new_raw = raw
    for it in items:
        base = it["base"]
        pat = old_url_pattern(base)
        hits = pat.findall(raw)
        print(f"  - {base} ({it['size_mb']} MB): {len(hits)} reference(s) in content")
        if not hits:
            print("    (no refs found — skipping)")
            continue
        # 1. download original (also = local backup) — cached between runs
        orig_path = os.path.join(BACKUPS, f"{base}.original.png")
        jpg_path = os.path.join(BACKUPS, f"{stem_of(base)}.jpg")
        os.makedirs(BACKUPS, exist_ok=True)
        if os.path.exists(orig_path) and os.path.exists(jpg_path):
            data = open(jpg_path, "rb").read()
            q = "cached"
        else:
            img_resp = c.get(it["image"])
            img_resp.raise_for_status()
            with open(orig_path, "wb") as fh:
                fh.write(img_resp.content)
            # infographics are text-heavy — start higher so labels stay crisp
            start_q = 90 if "infographic" in base or "chart" in base or "diagram" in base else 85
            data, q = to_jpeg(img_resp.content, start_q)
            with open(jpg_path, "wb") as fh:
                fh.write(data)
        plan.append({"item": it, "jpeg": data, "quality": q, "hits": len(hits),
                     "new_kb": round(len(data) / 1024)})

    if mode == "plan":
        print("\nPLAN (no writes made):")
        tot_old = tot_new = 0
        for p in plan:
            tot_old += p["item"]["size_mb"] * 1e6
            tot_new += p["new_kb"] * 1024
            print(f"  upload {stem_of(p['item']['base'])}.jpg  q{p['quality']}  {p['new_kb']} KB "
                  f"(was {p['item']['size_mb']} MB) · {p['hits']} ref(s) repointed")
        print(f"  page image weight: {round(tot_old/1e6, 2)} MB -> {round(tot_new/1e6, 2)} MB")
        print("run again with 'apply' to execute (single content write)")
        return

    if mode == "apply":
        snap_path = os.path.join(BACKUPS, f"post-{post_id}.before.html")
        with open(snap_path, "w", encoding="utf-8") as fh:
            fh.write(raw)
        print(f"snapshot: {snap_path}")
        for p in plan:
            it, base = p["item"], p["item"]["base"]
            # copy metadata from the original attachment; size-variant files are
            # not separate attachments — fall back to the parent image's record
            orig_att = None
            parent_stem = re.sub(r"-\d+x\d+$", "", stem_of(base)).lower()
            if it.get("attachment_id"):
                orig_att = wp_get(c, f"/media/{it['attachment_id']}")
            else:
                media = wp_list_all(c, "/media", "id,source_url")
                m = next((x for x in media
                          if re.sub(r"\.[a-z]+$", "", (x.get("source_url") or "").rsplit("/", 1)[-1]).lower() == parent_stem), None)
                if m:
                    orig_att = wp_get(c, f"/media/{m['id']}")
            meta = (orig_att or {})
            fallback_title = parent_stem.replace("-", " ")
            # upload JPEG as NEW attachment
            fname = stem_of(base) + ".jpg"
            r = c.post(
                "/media",
                files={"file": (fname, p["jpeg"], "image/jpeg")},
                data={
                    "title": meta.get("title", {}).get("raw") or fallback_title,
                    "caption": meta.get("caption", {}).get("raw", ""),
                    "description": meta.get("description", {}).get("raw", ""),
                    "alt_text": meta.get("alt_text", ""),
                    "post": str(post_id),
                },
            )
            if r.status_code not in (200, 201):
                sys.exit(f"media upload failed for {fname}: {r.status_code} {r.text[:300]}")
            att = r.json()
            new_url = att["source_url"]
            time.sleep(PACE)
            # repoint every old URL (any size variant) to the new file
            new_raw = old_url_pattern(base).sub(new_url, new_raw)
            print(f"  uploaded {fname} -> id {att['id']} ({p['new_kb']} KB, q{p['quality']})")
            p["new_id"], p["new_url"] = att["id"], new_url
        # single content write
        r = c.put(f"/{rest_base}/{post_id}", json={"content": new_raw})
        r.raise_for_status()
        # verify: re-read; every referenced upload URL must resolve 200 and no
        # old-pattern refs may remain (WP may rename files on upload — check
        # URLs, not expected names)
        _, check = get_post_raw(c, post_id, rest_base)
        leftovers = [p["item"]["base"] for p in plan
                     if old_url_pattern(p["item"]["base"]).search(check)]
        urls = sorted(set(re.findall(
            r"https://[^\s\"'\\<>)]*/uploads/[^\s\"'\\<>)]+\.(?:jpg|jpeg|png|webp)", check)))
        bad = []
        for u in urls:
            try:
                h = c.head(u)
                if h.status_code != 200:
                    bad.append((u.rsplit("/", 1)[-1], h.status_code))
            except Exception as e:
                bad.append((u.rsplit("/", 1)[-1], str(e)[:40]))
            time.sleep(0.2)
        ok = not bad and not leftovers
        print(f"content written. every referenced image resolves 200: {not bad}"
              f"{'; BAD: ' + str(bad) if bad else ''}; leftover old refs: {leftovers or 'none'}")
        # ledger update
        for p in plan:
            p["item"]["status"] = "applied" if ok and not leftovers else "applied-unverified"
            p["item"]["applied"] = {"post_id": post_id, "new_id": p.get("new_id"),
                                    "new_url": p.get("new_url"), "new_kb": p["new_kb"],
                                    "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        json.dump(ledger, open(LEDGER, "w"), indent=1)
        print(f"ledger updated ({LEDGER}). revert any time: "
              f"image_review_apply.py revert --page {post_id}")


if __name__ == "__main__":
    main()
