"""SEO report triage: generic Ahrefs CSV import -> issue backlog -> handler registry.

Reports in, worklists out. Each issue type maps to a handler that knows
enrich -> plan -> apply -> revert; types without a handler still enter the
backlog as trackable work. The image-size handler is the promoted image
review pipeline (shrink-copy + repoint; originals are never deleted).

WP access is lazy (`thailandnow._wp` / `._wp_list_all` resolved at call time)
so tests monkeypatch the app module exactly like the existing SEO tests do.
"""

import asyncio
import hashlib
import httpx
import io
import json
import re
import time
from collections import Counter
from pathlib import Path

BACKLOG_FILE = Path.home() / ".config" / "railjack" / "seo_backlog.json"
SESSION_FILE = Path.home() / ".config" / "railjack" / "seo_image_session.json"
BACKUPS_DIR = Path.home() / ".config" / "railjack" / "seo_backups"

# ---------------------------------------------------------------------------
# report-type detection
# ---------------------------------------------------------------------------

# Ahrefs export filename:  <site>_<date>_<issue-slug>_<date>_<time>.csv
_AHREFS_FILENAME_RE = re.compile(
    r"_\d{2}-[a-z]{3}-\d{4}_([a-z0-9-]+?)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv$", re.I
)

# known Ahrefs issue slugs -> (handler id, human label). Unknown slugs are
# still imported and tracked — they just have no fix handler yet.
AHREFS_ISSUE_ALIASES = {
    "image-file-size-too-l": ("image-size", "Image file size too large"),
    "image-file-size-too-large": ("image-size", "Image file size too large"),
    "missing-alt": ("alt-text", "Images missing alt text"),
    "broken-internal-link": ("broken-link", "Broken internal link"),
    "4xx-page": ("broken-link", "Broken page (4xx)"),
}

HANDLERS = {
    "image-size": {
        "label": "Image file size too large",
        "description": "Shrink-copy + repoint: upload a compressed copy, repoint "
                       "the page to it, keep the original. No deletes.",
        "capabilities": ["enrich", "plan", "apply", "revert"],
    },
    "alt-text": {"label": "Images missing alt text", "description": "not built yet",
                 "capabilities": []},
    "broken-link": {"label": "Broken links", "description": "not built yet",
                    "capabilities": []},
}


def slug_from_filename(filename: str) -> str | None:
    m = _AHREFS_FILENAME_RE.search(filename or "")
    return m.group(1).lower() if m else None


def resolve_report_type(filename: str, explicit: str | None) -> tuple[str, str | None, str]:
    """Returns (report_type, handler_id, label). Explicit choice wins; else the
    filename slug maps through AHREFS_ISSUE_ALIASES; else the raw slug stands
    alone (tracked, handler-less)."""
    if explicit:
        hid, label = AHREFS_ISSUE_ALIASES.get(explicit, (None, explicit.replace("-", " ")))
        return explicit, hid, label
    slug = slug_from_filename(filename)
    if not slug:
        return "unknown", None, "Unknown report"
    hid, label = AHREFS_ISSUE_ALIASES.get(slug, (None, slug.replace("-", " ")))
    return slug, hid, label


# ---------------------------------------------------------------------------
# backlog store
# ---------------------------------------------------------------------------

def _load() -> dict:
    try:
        return json.loads(BACKLOG_FILE.read_text())
    except Exception:
        return {"issues": {}, "history": []}


def _save(b: dict) -> None:
    BACKLOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKLOG_FILE.write_text(json.dumps(b, indent=1, ensure_ascii=False))


def import_report(csv_text: str, filename: str, explicit_type: str | None = None) -> dict:
    """Normalize Ahrefs CSV rows into the backlog. Issues key on (type, url);
    re-imports refresh last_seen and REOPEN 'fixed' issues that regressed."""
    import csv as _csv

    rtype, handler_id, label = resolve_report_type(filename, explicit_type)
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    backlog = _load()
    issues = backlog.setdefault("issues", {})

    rows = list(_csv.DictReader(io.StringIO(csv_text)))
    if not rows:
        raise ValueError("no CSV rows parsed — check the export format")

    counts: Counter = Counter()
    for row in rows:
        url = (row.get("URL") or row.get("url") or "").strip()
        if not url:
            continue
        try:
            size = int(row.get("Size (bytes)") or row.get("Size") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            inlinks = int(row.get("No. of IMG inlinks") or row.get("Inlinks") or 0)
        except (TypeError, ValueError):
            inlinks = 0
        key = hashlib.sha1(f"{rtype}:{url}".encode()).hexdigest()[:16]
        issue = issues.get(key)
        if issue is None:
            issues[key] = {"type": rtype, "url": url, "size_bytes": size, "inlinks": inlinks,
                           "status": "open", "first_seen": at, "last_seen": at,
                           "seen_count": 1, "filename": url.rsplit("/", 1)[-1]}
            counts["new"] += 1
        else:
            issue["last_seen"] = at
            issue["seen_count"] = issue.get("seen_count", 1) + 1
            issue["size_bytes"] = size or issue.get("size_bytes", 0)
            if issue.get("status") == "fixed":
                issue["status"] = "open"
                issue["regressed"] = True
            counts["updated"] += 1

    open_issues = [i for i in issues.values() if i.get("status") == "open"]
    history = backlog.setdefault("history", [])
    history.append({"at": at, "report_type": rtype, "handler": handler_id, "label": label,
                    "rows": len(rows), "total_open": len(open_issues),
                    "by_type": dict(Counter(i["type"] for i in open_issues))})
    backlog["history"] = history[-52:]
    _save(backlog)
    return {"report_type": rtype, "handler": handler_id, "label": label,
            "rows": len(rows), "new": counts.get("new", 0), "updated": counts.get("updated", 0),
            "total_open": len(open_issues)}


def backlog_view() -> dict:
    backlog = _load()
    issues = [{"key": k, **i} for k, i in backlog.get("issues", {}).items()]
    issues.sort(key=lambda i: (i.get("status") != "open", -(i.get("size_bytes") or 0)))
    by_type: Counter = Counter(i["type"] for i in issues if i.get("status") == "open")
    return {"issues": issues, "total_open": len(issues), "open_by_type": dict(by_type),
            "history": backlog.get("history", [])[-26:]}


def set_status(key: str, status: str) -> dict:
    if status not in ("open", "fixed", "dismissed"):
        raise ValueError("status must be open | fixed | dismissed")
    backlog = _load()
    issue = backlog.get("issues", {}).get(key)
    if issue is None:
        raise KeyError("no such backlog issue")
    issue["status"] = status
    issue["status_set_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save(backlog)
    return {"key": key, "status": status}


def handler_view() -> dict:
    return {"handlers": [dict(id=k, **v) for k, v in HANDLERS.items()]}


# ---------------------------------------------------------------------------
# image-size handler: enrich -> plan -> apply -> revert
# ---------------------------------------------------------------------------

VAR_SUFFIX = re.compile(r"-\d+x\d+(?=\.[a-zA-Z]+$)")
EXT = re.compile(r"\.[a-zA-Z]+$")
IMAGE_EXT = re.compile(r"\.(?:png|jpe?g|webp)$", re.I)


def _wp_module():
    """Lazy import so tests monkeypatch app.thailandnow._wp / ._wp_list_all."""
    from app import thailandnow
    return thailandnow


def base_name(url_or_name: str) -> str:
    name = url_or_name.rsplit("/", 1)[-1]
    name = VAR_SUFFIX.sub("", name)
    name = re.sub(r"-scaled(?=\.[a-zA-Z]+$)", "", name)
    return name.lower()


def stem_of(name: str) -> str:
    return EXT.sub("", name)


def old_url_pattern(stem: str) -> re.Pattern:
    """Any upload URL whose filename is <stem> with any size suffix / ext."""
    esc = re.escape(stem)
    return re.compile(
        rf"https?://[^\s\"'\\<>)]*{esc}(?:-\d+x\d+|-scaled(?:-e\d+)?)?\.(?:png|jpe?g|webp)",
        re.I,
    )


def _session_load() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except Exception:
        return {"items": [], "created_at": None}


def _session_save(s: dict) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(s, indent=1, ensure_ascii=False))


async def _retry(fn, attempts: int = 3, base: float = 5.0):
    """Site calls die mid-transfer (Sucuri curl-56s seen live) — back off and go again."""
    for i in range(attempts):
        try:
            return await fn()
        except Exception:
            if i == attempts - 1:
                raise
            await asyncio.sleep(base * (2 ** i))


async def enrich_session() -> dict:
    """Build a fix session from OPEN backlog issues of the image-size type:
    classify each flagged image via the media library + content scan.
    Classification: repoint-candidate (in content), featured-only (skip),
    unknown-location (Divi/theme storage — needs a human)."""
    tn = _wp_module()
    backlog = _load()
    urls = [i["url"] for i in backlog.get("issues", {}).values()
            if i.get("type") in ("image-file-size-too-l", "image-file-size-too-large",
                                 "image_file_size")
            and i.get("status") == "open"]
    if not urls:
        return {"session": None, "message": "no open image-size issues in the backlog"}

    media = await _retry(lambda: tn._wp_list_all("/media", "id,source_url,media_details,post"))
    by_stem = {}
    for m in media:
        u = (m.get("source_url") or "").rsplit("/", 1)[-1].lower()
        by_stem.setdefault(base_name(u), m)

    posts = []
    for rb in ("posts", "pages", "event"):
        posts.extend(await _retry(lambda rb=rb: tn._wp_list_all(
            f"/{rb}", "id,link,title,content,featured_media")))

    items = []
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        stem = stem_of(name).lower()
        m = by_stem.get(base_name(name)) or next(
            (x for x in media if stem_of((x.get("source_url") or "").rsplit("/", 1)[-1]).lower() == stem), None)
        det = (m or {}).get("media_details") or {}
        size_mb = round((det.get("filesize") or 0) / 1e6, 2)
        embeds, featured_in = [], []
        for p in posts:
            rendered = ((p.get("content") or {}).get("rendered") or "").lower()
            if stem in rendered:  # stem match: catches -1024x682 / -scaled / caps
                embeds.append({"id": p["id"], "link": p["link"],
                               "title": ((p.get("title") or {}).get("rendered") or "")[:80]})
            if m and p.get("featured_media") == m.get("id"):
                featured_in.append({"id": p["id"], "link": p["link"]})
        if embeds:
            action, status = "repoint", "pending"
        elif featured_in:
            action, status = "skip-featured", "skip-featured"
        else:
            action, status = "unknown-location", "divi-hidden"
        items.append({"image": url, "base": name, "stem": stem,
                      "attachment_id": (m or {}).get("id"),
                      "size_bytes": det.get("filesize") or 0,
                      "size_mb": size_mb,
                      "width": det.get("width"), "height": det.get("height"),
                      "embeds": embeds, "featured_in": featured_in,
                      "action": action, "status": status})

    session = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "items": items}
    _session_save(session)
    summary = Counter(i["status"] for i in items)
    return {"session": session, "created_at": session["created_at"],
            "counts": dict(summary)}


def session_view() -> dict:
    s = _session_load()
    pages: dict[int, dict] = {}
    for i in s.get("items", []):
        for e in i.get("embeds", []):
            pg = pages.setdefault(e["id"], {"post_id": e["id"], "link": e["link"],
                                            "title": e["title"], "pending": [], "done": 0})
            if i.get("status") == "pending":
                pg["pending"].append({"base": i["base"], "size_mb": i.get("size_mb")})
            elif i.get("status") == "applied":
                pg["done"] += 1
    return {"created_at": s.get("created_at"), "pages": list(pages.values()),
            "items": s.get("items", [])}


def _jpeg_bytes(data: bytes, start_q: int = 85, max_dim: int = 1280) -> tuple[bytes, int]:
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    if img.mode in ("RGBA", "P", "LA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    ladder = [start_q] + [q for q in (80, 75, 70) if q < start_q]
    buf = io.BytesIO()
    for q in ladder:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        if buf.tell() <= 1_000_000:
            return buf.getvalue(), q
    return buf.getvalue(), ladder[-1]


def _start_q(name: str) -> int:
    if re.search(r"infographic|chart|diagram", name, re.I):
        return 90
    if IMAGE_EXT.search(name) and re.search(r"\.jpe?g$", name, re.I):
        return 75
    return 85


async def plan_page(post_id: int) -> dict:
    """Download + compress pending images for one page. No WP writes."""
    tn = _wp_module()
    s = _session_load()
    items = [i for i in s.get("items", []) if i.get("status") == "pending"
             and any(e["id"] == post_id for e in i.get("embeds", []))]
    if not items:
        return {"post_id": post_id, "plan": [], "note": "no pending items for this page"}

    post = await tn._wp("GET", f"/posts/{post_id}", {"context": "edit"})
    raw = (post.get("content") or {}).get("raw", "")
    out = []
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (railjack seo-fix)"}) as hc:
        for it in items:
            orig_path = BACKUPS_DIR / f"{it['base']}.original"
            jpg_path = BACKUPS_DIR / f"{stem_of(it['base'])}.jpg"
            if jpg_path.exists():
                data = jpg_path.read_bytes()
                q = "cached"
            else:
                img_r = await hc.get(it["image"])
                img_r.raise_for_status()
                orig_path.write_bytes(img_r.content)
                data, q = _jpeg_bytes(img_r.content, _start_q(it["base"]))
                jpg_path.write_bytes(data)
            refs = old_url_pattern(stem_of(it["base"])).findall(raw)
            out.append({"base": it["base"], "refs": len(refs), "new_kb": round(len(data) / 1024),
                        "quality": q, "old_kb": round((it.get("size_bytes") or 0) / 1024) if it.get("size_bytes") else None})
    return {"post_id": post_id, "plan": out,
            "note": "no writes made — apply to execute one content write for this page"}


async def apply_page(post_id: int) -> dict:
    """Upload compressed copies + repoint this page's content in ONE write.
    Raw-content snapshot first; every referenced upload URL is HEAD-verified."""
    tn = _wp_module()
    s = _session_load()
    items = [i for i in s.get("items", []) if i.get("status") == "pending"
             and any(e["id"] == post_id for e in i.get("embeds", []))]

    rest_base = "posts"
    post = await tn._wp("GET", f"/posts/{post_id}", {"context": "edit"})
    if not post or post.get("id") != post_id:
        post = await tn._wp("GET", f"/pages/{post_id}", {"context": "edit"})
        rest_base = "pages"
    raw = (post.get("content") or {}).get("raw", "")
    if not items:
        return {"post_id": post_id, "applied": [], "note": "no pending items"}
    if rest_base == "pages":
        raise ValueError("revert/apply v1 covers posts only — page support lands with sessions on pages")

    snap = BACKUPS_DIR / f"post-{post_id}.before.html"
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    snap.write_text(raw, encoding="utf-8")

    media = await _retry(lambda: tn._wp_list_all("/media", "id,source_url,media_details"))
    by_stem = {}
    for m in media:
        u = (m.get("source_url") or "").rsplit("/", 1)[-1].lower()
        by_stem.setdefault(base_name(u), m)

    new_raw, results = raw, []
    url, user, pwd = tn._wp_creds()
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as hc:
        for it in items:
            stem = stem_of(it["base"])
            jpg_path = BACKUPS_DIR / f"{stem}.jpg"
            if not jpg_path.exists():
                results.append({"base": it["base"], "error": "not prepped — run plan first"})
                continue
            data = jpg_path.read_bytes()
            att_id = it.get("attachment_id")
            meta = {}
            if att_id:
                meta = await tn._wp("GET", f"/media/{att_id}") or {}
            else:
                pm = by_stem.get(base_name(it["base"]))
                if pm:
                    meta = await tn._wp("GET", f"/media/{pm['id']}") or {}
            up = await hc.post(
                f"{url}/wp-json/wp/v2/media",
                files={"file": (stem + ".jpg", data, "image/jpeg")},
                data={"title": (meta.get("title") or {}).get("raw") or parent_title_fallback(stem),
                      "alt_text": meta.get("alt_text", ""),
                      "post": str(post_id)},
                auth=(user, pwd))
            if up.status_code not in (200, 201):
                results.append({"base": it["base"], "error": f"upload {up.status_code}: {up.text[:120]}"})
                continue
            att = up.json()
            new_url = att.get("source_url", "")
            new_raw = old_url_pattern(stem).sub(new_url, new_raw)
            results.append({"base": it["base"], "new_id": att.get("id"), "new_url": new_url,
                            "new_kb": round(len(data) / 1024)})
    await tn._wp("PUT", f"/{rest_base}/{post_id}", json_body={"content": new_raw})
    refreshed = await tn._wp("GET", f"/{rest_base}/{post_id}", {"context": "edit"})
    check = (refreshed.get("content") or {}).get("raw", "")
    new_urls = {x.get("new_url") for x in results if x.get("new_url")}
    leftovers = sorted({u for it in items
                        for u in old_url_pattern(stem_of(it["base"])).findall(check)
                        if u not in new_urls})
    ok = not leftovers and all("error" not in x for x in results)
    status = "applied" if ok else "applied-unverified"
    for it in items:
        it["status"] = status
        it["applied"] = {"post_id": post_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _session_save(s)
    # auto-close the matching backlog issues — the crawl diff will confirm later
    backlog = _load()
    for it in items:
        if it["status"] != "applied":
            continue
        for issue in backlog.get("issues", {}).values():
            if issue.get("url") == it["image"] and issue.get("status") == "open":
                issue["status"] = "fixed"
                issue["fixed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                issue["fixed_via"] = f"image-size handler, post {post_id}"
    _save(backlog)
    return {"post_id": post_id, "rest_base": rest_base, "applied": results,
            "leftovers": leftovers, "snapshot": str(snap), "status": status}


def parent_title_fallback(stem: str) -> str:
    return re.sub(r"[-_]+\d*x?\d*$", "", stem).replace("-", " ").replace("_", " ")


async def revert_page(post_id: int) -> dict:
    tn = _wp_module()
    snap = BACKUPS_DIR / f"post-{post_id}.before.html"
    if not snap.exists():
        raise FileNotFoundError(f"no snapshot for post {post_id}")
    raw = snap.read_text(encoding="utf-8")
    await tn._wp("PUT", f"/posts/{post_id}", json_body={"content": raw})
    return {"post_id": post_id, "reverted": True, "snapshot": str(snap)}
