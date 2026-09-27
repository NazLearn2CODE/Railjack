"""SEO site-audit extensions: sitemap hygiene, oversized media, redirecting
internal links, orphan parity (pages + chrome), and the href-rewrite fixer.

Everything rides fakes — ``_wp``/``_wp_list_all``/``_seo_fetch_text`` monkeypatched,
HTTP probes via ``httpx.MockTransport`` injected into ``httpx.AsyncClient``. Nothing
touches the real site.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import thailandnow
from app.main import app


@pytest.fixture(autouse=True)
def _no_jev_meter(monkeypatch, tmp_path):
    """Machine-independent tests: JEV gate degrades to no-gate, never the real meter."""
    from app import jev_gates
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "no-meter.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def _fresh_content_cache(monkeypatch):
    """The content snapshot is a module global — tests must not leak into each other."""
    monkeypatch.setattr(thailandnow, "_SEO_CONTENT_CACHE", None)


@pytest.fixture(autouse=True)
def _no_curl_fallback(monkeypatch):
    """Tests fake everything through httpx mocks — the real curl_cffi fallback must
    never fire (it would escape to the network). Re-enabled per-test explicitly."""
    monkeypatch.setattr(thailandnow, "_CURL_OK", False)


SITE = "https://www.thailandnow.in.th"


@pytest.fixture
def client():
    return TestClient(app)


def _mock_client(monkeypatch, handler):
    """Route every httpx.AsyncClient the module opens through a MockTransport."""
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(**kw):
        kw["transport"] = transport
        return real(**kw)

    monkeypatch.setattr(thailandnow.httpx, "AsyncClient", factory)


# ---------------------------------------------------------------- pure fns ---

def test_noindex_reason_meta_both_orders_and_header():
    a = '<html><head><meta name="robots" content="noindex, follow"></head></html>'
    b = '<html><head><meta content="noindex, nofollow" name="robots"></head></html>'
    assert thailandnow._seo_noindex_reason(a) == "meta robots"
    assert thailandnow._seo_noindex_reason(b) == "meta robots"
    assert thailandnow._seo_noindex_reason("<p>clean</p>", "noindex") == "X-Robots-Tag"
    assert thailandnow._seo_noindex_reason(a, "noindex") == "meta robots + X-Robots-Tag"
    assert thailandnow._seo_noindex_reason('<meta name="robots" content="index, follow">') == ""


def test_rewrite_href_exact_and_normalized():
    html = '<p>see <a href="/old-slug/">old</a> and <a href="/old-slug/">again</a></p>'
    new_html, matches, before, after = thailandnow._seo_rewrite_href(html, "/old-slug/", "/new-slug/")
    assert matches == 2
    assert new_html.count('href="/new-slug/"') == 2 and "old-slug" not in new_html
    assert "/new-slug/" in after and "/new-slug/" not in before
    # normalized: trailing-slash variance still matches (norm strips it)
    html2 = '<p>x <a href="/old-slug">y</a></p>'
    new2, m2, _, _ = thailandnow._seo_rewrite_href(html2, "/old-slug/", "/new-slug/")
    assert m2 == 1 and 'href="/new-slug/"' in new2
    # no match → untouched
    html3 = "<p>no links</p>"
    assert thailandnow._seo_rewrite_href(html3, "/old/", "/new/") == (html3, 0, "", "")


def test_rewrite_href_keeps_other_attributes():
    html = '<p><a class="btn" target="_blank" href="/old/" rel="noopener">x</a></p>'
    new_html, matches, _, _ = thailandnow._seo_rewrite_href(html, "/old/", "/new/")
    assert matches == 1
    assert 'class="btn"' in new_html and 'rel="noopener"' in new_html and 'href="/new/"' in new_html


# ------------------------------------------------- orphan parity (pure) -----

def _rec(id, path, content=""):
    return {"id": id, "link": f"{SITE}{path}",
            "title": {"rendered": f"Rec {id}"}, "content": {"rendered": content}}


def test_orphan_report_counts_pages_and_honours_chrome():
    posts = [_rec(1, "/linked-post/", '<a href="/target-post/">t</a>'),
             _rec(2, "/target-post/", "<p>x</p>")]
    pages = [_rec(3, "/lonely-page/", "<p>p</p>"),  # zero inbound anywhere → orphan
             _rec(4, "/footer-page/", "<p>f</p>")]  # linked only from chrome → NOT orphan
    rep = thailandnow._seo_internal_report(
        posts, pages, [], [], set(), set(), "www.thailandnow.in.th",
        chrome_inbound={"/footer-page/"})
    opaths = {thailandnow._seo_path(o["link"]) for o in rep["orphans"]}
    assert "/lonely-page" in opaths           # pages are orphan-eligible now
    assert "/footer-page" not in opaths       # chrome link rescues it
    assert "/target-post" not in opaths       # inbound from post content


# ------------------------------------------------------------ sitemap fns ---

def test_sitemap_urls_index_expansion_and_foreign_filter(monkeypatch):
    bodies = {
        SITE + "/wp-sitemap.xml":
            "<sitemapindex>"
            '<sitemap><loc>https://www.thailandnow.in.th/wp-sitemap-posts-post-1.xml</loc></sitemap>'
            '<sitemap><loc>https://cdn.example.com/wp-sitemap-pages-page-1.xml</loc></sitemap>'
            "</sitemapindex>",
        SITE + "/wp-sitemap-posts-post-1.xml":
            "<urlset><url><loc>https://www.thailandnow.in.th/post-a/</loc></url>"
            "<url><loc>https://www.thailandnow.in.th/post-b/</loc></url></urlset>",
        "https://cdn.example.com/wp-sitemap-pages-page-1.xml":
            "<urlset><url><loc>https://cdn.example.com/foreign-page/</loc></url></urlset>",
    }

    async def fake_fetch(url, sem, timeout, max_bytes=2_000_000):
        return bodies[url]

    monkeypatch.setattr(thailandnow, "_seo_fetch_text", fake_fetch)
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE + "/", "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp_site_host", lambda: "www.thailandnow.in.th")
    urls, note = asyncio.run(thailandnow._seo_sitemap_urls())
    assert urls == [f"{SITE}/post-a/", f"{SITE}/post-b/"]  # child expanded, foreign dropped
    assert note == ""


def test_sitemap_hygiene_buckets(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/redir/":
            return httpx.Response(301, headers={"location": "/final/"})
        if p == "/final/":
            return httpx.Response(200, text="<html><body>ok</body></html>")
        if p == "/noindex-meta/":
            return httpx.Response(200, text='<meta name="robots" content="noindex">')
        if p == "/noindex-header/":
            return httpx.Response(200, headers={"X-Robots-Tag": "noindex"}, text="<p>x</p>")
        if p == "/gone/":
            return httpx.Response(404, text="gone")
        if p == "/blocked/":
            return httpx.Response(403, text="no")
        if p == "/selfloop/":  # Sucuri challenge signature: 307 pointing at itself
            return httpx.Response(307, headers={"location": "/selfloop/"})
        return httpx.Response(200, text="<p>fine</p>")

    _mock_client(monkeypatch, handler)
    base = SITE
    urls = [base + u for u in ("/redir/", "/noindex-meta/", "/noindex-header/",
                               "/gone/", "/blocked/", "/fine/", "/selfloop/")]
    h = asyncio.run(thailandnow._seo_sitemap_hygiene(urls))
    assert h["checked"] == 7
    assert len(h["redirects"]) == 1
    r = h["redirects"][0]
    assert r["url_path"] == "/redir" and r["final_path"] == "/final"
    assert r["final_url"] == base + "/final/"
    assert {e["url_path"] for e in h["noindex"]} == {"/noindex-meta", "/noindex-header"}
    assert {e["url_path"] for e in h["broken"]} == {"/gone"}
    assert {e["url_path"] for e in h["manual"]} == {"/blocked", "/selfloop"}
    loop = [m for m in h["manual"] if m["url_path"] == "/selfloop"][0]
    assert "WAF challenge" in loop["reason"]
    assert h["challenge_loops"] == 1
    assert h["ok"] == [base + "/fine/"]


def test_oversized_media_filters_mime_and_threshold(monkeypatch):
    async def fake_list(endpoint, fields):
        assert endpoint == "/media"
        return [
            {"id": 1, "source_url": f"{SITE}/big.jpg", "mime_type": "image/jpeg",
             "media_details": {"filesize": 2_500_000,
                               "sizes": {"large": {"source_url": f"{SITE}/big-1024x576.jpg"},
                                         "medium": {"source_url": f"{SITE}/big-300x169.jpg"}}}},
            {"id": 2, "source_url": f"{SITE}/small.jpg", "mime_type": "image/jpeg",
             "media_details": {"filesize": 400_000, "sizes": {}}},
            {"id": 3, "source_url": f"{SITE}/huge.mp4", "mime_type": "video/mp4",
             "media_details": {"filesize": 90_000_000, "sizes": {}}},
            {"id": 4, "source_url": f"{SITE}/offloaded.png", "mime_type": "image/png",
             "media_details": {}},  # offloaded — no filesize → can't weigh
            {"id": 5, "source_url": f"{SITE}/nosize.png", "mime_type": "image/png",
             "media_details": {"filesize": 3_000_000, "sizes": {}}},  # oversized, no variants
        ]

    monkeypatch.setattr(thailandnow, "_wp_list_all", fake_list)
    out, note = asyncio.run(thailandnow._seo_oversized_media())
    assert note == ""
    assert out == [
        {"id": 5, "source_url": f"{SITE}/nosize.png", "filesize": 3_000_000, "shrink_to": ""},
        {"id": 1, "source_url": f"{SITE}/big.jpg", "filesize": 2_500_000,
         "shrink_to": f"{SITE}/big-1024x576.jpg"},
    ]


def test_rewrite_img_src_exact_normalized_and_srcset_untouched():
    html = ('<figure><img src="%s/big.jpg" srcset="%s/big-300x169.jpg 300w"> '
            '<img class="x" src="%s/big.jpg"></figure>' % (SITE, SITE, SITE))
    new_html, matches, _, _ = thailandnow._seo_rewrite_img_src(
        html, f"{SITE}/big.jpg", f"{SITE}/big-1024x576.jpg")
    assert matches == 2
    assert new_html.count(f'"{SITE}/big-1024x576.jpg"') == 2  # src + the rewrite… srcset untouched
    assert "big-300x169.jpg 300w" in new_html
    # normalized fallback (trailing-slash/scheme variance is out of scope for files,
    # but www/scheme normalization still matches)
    html2 = '<img src="https://www.thailandnow.in.th/big.jpg">'
    new2, m2, _, _ = thailandnow._seo_rewrite_img_src(html2, f"{SITE}/big.jpg", f"{SITE}/s.jpg")
    assert m2 == 1 and 'src="https://www.thailandnow.in.th/s.jpg"' in new2
    assert thailandnow._seo_rewrite_img_src("<p>x</p>", "/a.jpg", "/b.jpg") == ("<p>x</p>", 0, "", "")


def test_oversized_media_degrades_on_non_json(monkeypatch):
    """Live-scan scar (2026-09-26): a non-JSON 2xx media listing must not kill the
    scan — it degrades to [] WITH a note (never looks like 'no oversized images')."""
    async def boom(endpoint, fields):
        raise ValueError("Expecting value: line 1 column 10 (char 9)")

    monkeypatch.setattr(thailandnow, "_wp_list_all", boom)
    out, note = asyncio.run(thailandnow._seo_oversized_media())
    assert out == [] and "media listing failed" in note


def test_chrome_inbound_parses_home_and_archives(monkeypatch):
    bodies = {
        SITE + "/": '<nav><a href="/menu-post/">m</a><a href="https://else.example.com/x/">e</a></nav>',
        SITE + "/page/2/": '<a href="/paged-post/">p</a>',
        SITE + "/page/3/": "",  # fetch gave nothing — skipped
        SITE + "/cat/food/": '<a href="/archive-only/">a</a>',
    }

    async def fake_fetch(url, sem, timeout, max_bytes=2_000_000):
        return bodies.get(url, "")

    monkeypatch.setattr(thailandnow, "_seo_fetch_text", fake_fetch)
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE + "/", "u", "p"))
    linked, fetched, attempted = asyncio.run(
        thailandnow._seo_chrome_inbound({"/cat/food/"}, "www.thailandnow.in.th"))
    assert linked == {"/menu-post", "/paged-post", "/archive-only"}
    assert (fetched, attempted) == (4, 4)


# ------------------------------------------------------------- endpoints ----

class _FakeWP:
    def __init__(self, contents, missing_ids=()):
        self.contents = dict(contents)
        self.missing_ids = set(missing_ids)
        self.puts = []

    async def __call__(self, method, path, params=None, json_body=None):
        pid = int(path.strip("/").split("/")[1])
        if pid in self.missing_ids:
            raise HTTPException(404, f"WP record {pid} not found")
        if method == "GET":
            return {"id": pid, "link": f"{SITE}/rec-{pid}/",
                    "content": {"raw": self.contents.get(pid, "")}}
        self.contents[pid] = json_body["content"]  # writes persist — idempotency is testable
        self.puts.append((pid, json_body["content"]))
        return {"id": pid}


@pytest.fixture
def fake_wp(monkeypatch):
    async def resolve(post_id):
        return "posts"

    monkeypatch.setattr(thailandnow, "_wp_creds",
                        lambda: (SITE, "mock_user", "mock_pwd"))
    monkeypatch.setattr(thailandnow, "_wp_resolve_rest_base", resolve)

    def _install(contents, missing_ids=()):
        wp = _FakeWP(contents, missing_ids)
        monkeypatch.setattr(thailandnow, "_wp", wp)
        return wp

    return _install


def test_preview_rewrite_no_write(client, fake_wp):
    fake_wp({7: '<p>a <a href="/old/">link</a></p>'})
    r = client.post("/api/thailandnow/seo/preview-rewrite",
                    json={"post_id": 7, "old_href": "/old/", "new_url": "/new/"})
    assert r.status_code == 200
    d = r.json()
    assert d["matches"] == 1 and 'href="/new/"' in d["after"]


def test_apply_rewrite_writes_and_noops(client, fake_wp):
    wp = fake_wp({7: '<p>a <a href="/old/">link</a> <a href="/old/">two</a></p>'})
    r = client.post("/api/thailandnow/seo/apply-rewrite",
                    json={"post_id": 7, "old_href": "/old/", "new_url": "/new/"})
    assert r.status_code == 200 and r.json()["matches"] == 2
    assert len(wp.puts) == 1 and wp.puts[0][0] == 7
    assert wp.puts[0][1].count('href="/new/"') == 2
    # idempotent no-op: nothing left to rewrite, no second write
    r2 = client.post("/api/thailandnow/seo/apply-rewrite",
                     json={"post_id": 7, "old_href": "/old/", "new_url": "/new/"})
    assert r2.json()["matches"] == 0 and len(wp.puts) == 1


def test_apply_rewrite_bulk_counts(client, fake_wp):
    fake_wp({7: '<p><a href="/old/">x</a></p>', 8: "<p>nothing</p>"}, missing_ids=(99,))
    r = client.post("/api/thailandnow/seo/apply-rewrite-bulk", json={"items": [
        {"post_id": 7, "old_href": "/old/", "new_url": "/new/"},
        {"post_id": 8, "old_href": "/old/", "new_url": "/new/"},
        {"post_id": 99, "old_href": "/old/", "new_url": "/new/"},
    ]})
    d = r.json()
    assert d["total"] == 3 and d["rewritten"] == 1 and d["noop"] == 1
    assert d["failed"] == 1 and len(d["results"]) == 3


def test_apply_fix_bulk_surfaces_error_sample(client, fake_wp, monkeypatch):
    """Live scar 2026-09-26: '29 failed' with zero reasons — every failure was the
    rate limiter and the UI swallowed it. Bulk responses now carry a distinct-error
    sample; inter-item pacing keeps the burst under the limiter."""
    monkeypatch.setattr(thailandnow, "_opts", lambda: {"seo_bulk_delay_s": 0})
    wp = fake_wp({7: '<p><img src="/x.jpg"></p>'}, missing_ids=(99,))
    r = client.post("/api/thailandnow/seo/apply-fix-bulk", json={"items": [
        {"post_id": 7, "kind": "image", "target": "/x.jpg"},
        {"post_id": 99, "kind": "image", "target": "/y.jpg"},
    ]})
    d = r.json()
    assert d["removed"] == 1 and d["failed"] == 1
    assert d["error_sample"] and "not found" in d["error_sample"][0]
    assert wp.puts  # the good item really wrote


def test_image_shrink_endpoints(client, fake_wp):
    wp = fake_wp({11: f'<p><img src="{SITE}/big.jpg" alt="a"></p>'})
    body = {"post_id": 11, "old_src": f"{SITE}/big.jpg", "new_src": f"{SITE}/big-1024x576.jpg"}
    d = client.post("/api/thailandnow/seo/preview-image-shrink", json=body).json()
    assert d["matches"] == 1 and "big-1024x576" in d["after"]
    r2 = client.post("/api/thailandnow/seo/apply-image-shrink", json=body)
    assert r2.json()["matches"] == 1 and len(wp.puts) == 1
    assert wp.puts[0][1].count("big-1024x576") == 1
    r3 = client.post("/api/thailandnow/seo/apply-image-shrink", json=body)
    assert r3.json()["matches"] == 0 and len(wp.puts) == 1  # idempotent


def test_suggest_retarget_ranks_by_overlap(client, monkeypatch):
    async def fake_list(endpoint, fields):
        if endpoint == "/posts":
            return [
                {"id": 1, "link": f"{SITE}/khon-kaen-street-food/",
                 "title": {"rendered": "Khon Kaen Street Food Guide"}},
                {"id": 2, "link": f"{SITE}/bangkok-malls/",
                 "title": {"rendered": "Bangkok Malls"}},
            ]
        return [{"id": 5, "link": f"{SITE}/about/", "title": {"rendered": "About Us"}}]

    monkeypatch.setattr(thailandnow, "_wp_list_all", fake_list)
    r = client.post("/api/thailandnow/seo/suggest-retarget",
                    json={"to": "/khon-kaen-night-market/",
                          "from_link": f"{SITE}/bangkok-weekend/",
                          "from_title": "Bangkok Weekend Guide"})
    s = r.json()["suggestions"]
    assert s and s[0]["link"] == f"{SITE}/khon-kaen-street-food/"
    assert len(s) <= 3
    # every suggestion carries an advisory JEV verdict (degrades to 'skipped'
    # under the test fixture's missing meter — never blocks the pick)
    assert all("jev" in x and x["jev"]["verdict"] in ("ok", "weak", "skipped") for x in s)


# ------------------------------------------------------------ flow wiring ---

def _flow_job():
    return SimpleNamespace(cancel=False, progress=0, result=None)


def test_flow_seo_health_report_wiring(monkeypatch):
    """Full HEALTH flow with faked WP + HTTP: orphans honour chrome, redirecting
    internal links land (probed + sitemap merge, deduped), hygiene + oversized ride."""
    posts = [
        _rec(1, "/linker/", '<p>see <a href="/old-slug/">old</a> and <a href="/target/">t</a>'
                            ' and <a href="/loop-page/">loop</a>'
                            f' and <img src="{SITE}/heavy.jpg"></p>'),
        _rec(2, "/target/", "<p>x</p>"),
        _rec(3, "/new-slug/", "<p>y</p>"),
    ]
    posts[1]["featured_media"] = 11  # feat.png is post 2's featured image
    pages = [_rec(4, "/orphan-page/", "<p>p</p>"), _rec(5, "/chrome-page/", "<p>c</p>")]

    async def fake_fetch_all():
        return posts, pages, [], [], set(), set()

    async def fake_chrome(extra, host):
        return {"/chrome-page"}, 1, 1  # fetched == attempted → no degradation note

    async def fake_sitemap_urls():
        return [f"{SITE}/old-slug/", f"{SITE}/ghost/", f"{SITE}/tag/khon-kaen/"], ""

    async def fake_hygiene(urls):
        return {"checked": 3,
                "redirects": [{"url": f"{SITE}/old-slug/", "url_path": "/old-slug",
                               "status": 301, "location": "/new-slug/",
                               "final_url": f"{SITE}/new-slug/", "final_path": "/new-slug"}],
                "noindex": [], "broken": [], "manual": [],
                "ok": [f"{SITE}/ghost/", f"{SITE}/tag/khon-kaen/"],
                "challenge_loops": 4}  # firewall challenged the sitemap probe

    async def fake_oversized():
        return [{"id": 9, "source_url": f"{SITE}/heavy.jpg", "filesize": 3_000_000,
                 "shrink_to": f"{SITE}/heavy-1024x576.jpg"},
                {"id": 10, "source_url": f"{SITE}/ghost.png", "filesize": 2_000_000,
                 "shrink_to": ""},
                {"id": 11, "source_url": f"{SITE}/feat.png", "filesize": 1_500_000,
                 "shrink_to": f"{SITE}/feat-1024x576.jpg"}], ""

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/old-slug/":
            return httpx.Response(301, headers={"location": "/new-slug/"})
        if p == "/loop-page/":  # WAF challenge: 307 to itself
            return httpx.Response(307, headers={"location": "/loop-page/"})
        return httpx.Response(200, text="<html><body>fine</body></html>")

    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp_site_host", lambda: "www.thailandnow.in.th")
    monkeypatch.setattr(thailandnow, "_seo_fetch_all", fake_fetch_all)
    monkeypatch.setattr(thailandnow, "_seo_chrome_inbound", fake_chrome)
    monkeypatch.setattr(thailandnow, "_seo_sitemap_urls", fake_sitemap_urls)
    monkeypatch.setattr(thailandnow, "_seo_sitemap_hygiene", fake_hygiene)
    monkeypatch.setattr(thailandnow, "_seo_oversized_media", fake_oversized)
    _mock_client(monkeypatch, handler)

    job = _flow_job()
    asyncio.run(thailandnow._flow_seo_health(job))
    res = job.result

    # orphans: pages count; chrome-rescued page excluded
    opaths = {thailandnow._seo_path(o["link"]) for o in res["orphans"]}
    assert "/orphan-page" in opaths and "/chrome-page" not in opaths
    # redirecting internal links: probed (301) + sitemap merge deduped to ONE entry;
    # the 307-to-self loop is a challenge artifact → manual, never a redirect
    rd = res["redirecting_internal_links"]
    assert len(rd) == 1
    assert rd[0]["from_id"] == 1 and rd[0]["status"] == 301
    assert rd[0]["final_path"] == "/new-slug"
    loop = [m for m in res["internal_manual_check"] if "/loop-page" in (m.get("to") or "")]
    assert len(loop) == 1 and "WAF challenge" in loop[0]["reason"]
    # hygiene wired through + unlinked = 200s that are neither content records nor
    # chrome-linked — ghost page AND the tag archive (the Ahrefs-orphan universe)
    assert res["sitemap"]["checked"] == 3
    assert res["sitemap"]["unlinked"]["count"] == 2
    assert res["sitemap"]["unlinked"]["urls"] == [f"{SITE}/ghost/", f"{SITE}/tag/khon-kaen/"]
    assert "ok" not in res["sitemap"] and "ok_count" in res["sitemap"]
    assert res["scan_notes"] != []  # 4 challenge loops ≥ 3 → firewall warning surfaced
    assert any("looped a redirect to themselves" in n for n in res["scan_notes"])
    # internals popped, oversized present with per-post usage attribution
    assert "internal_link_pairs_all" not in res and "valid_paths_list" not in res
    assert "record_paths_list" not in res and "internal_img_pairs_all" not in res
    ov = {o["id"]: o for o in res["oversized_images"]}
    assert ov[9]["klass"] == "full-used"      # post 1 embeds the full file
    assert ov[9]["used_in"] == [{"from": f"{SITE}/linker/", "from_id": 1,
                                 "from_title": "Rec 1"}]
    assert ov[10]["klass"] == "unused"        # nothing embeds it → deletable
    assert ov[11]["klass"] == "featured"      # post 2's featured image → manual


def test_flow_scan_notes_surface_degraded_runs(monkeypatch):
    """A degraded run (chrome partially blocked, media listing failed) must surface
    scan_notes, never look like zero problems."""
    posts = [_rec(1, "/linker/", "<p>x</p>")]
    pages = [_rec(2, "/orphan-page/", "<p>p</p>")]

    async def fake_fetch_all():
        return posts, pages, [], [], set(), set()

    async def fake_chrome(extra, host):
        return set(), 3, 8  # 5 of 8 chrome pages failed to fetch

    async def fake_sitemap_urls():
        return [], "no sitemap (403 Forbidden on sitemap.xml)"

    async def fake_hygiene(urls):
        return {"checked": 0, "redirects": [], "noindex": [], "broken": [],
                "manual": [], "ok": []}

    async def fake_oversized():
        return [], "media listing failed (Expecting value) — oversized report unavailable"

    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp_site_host", lambda: "www.thailandnow.in.th")
    monkeypatch.setattr(thailandnow, "_seo_fetch_all", fake_fetch_all)
    monkeypatch.setattr(thailandnow, "_seo_chrome_inbound", fake_chrome)
    monkeypatch.setattr(thailandnow, "_seo_sitemap_urls", fake_sitemap_urls)
    monkeypatch.setattr(thailandnow, "_seo_sitemap_hygiene", fake_hygiene)
    monkeypatch.setattr(thailandnow, "_seo_oversized_media", fake_oversized)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, text="<p>ok</p>"))

    job = _flow_job()
    asyncio.run(thailandnow._flow_seo_health(job))
    res = job.result
    notes = " ".join(res["scan_notes"])
    assert "3/8" in notes and "sitemap" in notes and "media listing failed" in notes
    assert res["sitemap"]["checked"] == 0 and res["oversized_images"] == []


def test_rewrite_href_rejects_empty():
    assert thailandnow._seo_rewrite_href("<p>x</p>", "", "/new/") == ("<p>x</p>", 0, "", "")


def test_resolve_href_cleans_dot_segments():
    """Page-builder nav writes './arts-culture/' — must probe as /arts-culture/,
    never the malformed /.//arts-culture/ (live-scan scar 2026-09-26)."""
    base = SITE + "/"
    assert thailandnow._seo_resolve_href("./arts-culture/", base) == f"{SITE}/arts-culture/"
    assert thailandnow._seo_resolve_href("/events", base) == f"{SITE}/events"
    assert thailandnow._seo_resolve_href("foo/bar", base) == f"{SITE}/foo/bar"
    assert thailandnow._seo_resolve_href(f"{SITE}/x/", base) == f"{SITE}/x/"


def test_waf_cookie_same_site_only(monkeypatch):
    """Opt-in clearance cookie rides SAME-SITE requests only — never leaks to
    external domains a probe happens to touch."""
    monkeypatch.setattr(thailandnow, "_wp_site_host", lambda: "www.thailandnow.in.th")
    monkeypatch.setattr(thailandnow, "_opts",
                        lambda: {"seo_waf_cookie": "sucuri_cloudproxy_uuid_x=abc"})
    site = thailandnow._seo_site_headers(f"{SITE}/wp-sitemap.xml")
    assert site.get("Cookie") == "sucuri_cloudproxy_uuid_x=abc"
    assert "Cookie" not in thailandnow._seo_site_headers("https://external.example.com/x",
                                                         {"User-Agent": "ua"})
    # feature OFF when opts carry no cookie:
    monkeypatch.setattr(thailandnow, "_opts", lambda: {})
    assert "Cookie" not in thailandnow._seo_site_headers(f"{SITE}/x")


def test_wp_retries_429_with_backoff(monkeypatch):
    """Live scar 2026-09-26: LiteSpeed's plain 429 killed the scan on the first
    REST call. _wp must ride it out (Retry-After / staged backoff), not die."""
    monkeypatch.setattr(thailandnow, "_WP_429_BACKOFF_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(thailandnow.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, text="429 Too Many Requests")
        return httpx.Response(200, json=[{"id": 1}])

    _mock_client(monkeypatch, handler)
    out = asyncio.run(thailandnow._wp("GET", "/posts", {"per_page": 1}))
    assert out == [{"id": 1}] and calls["n"] == 3
    assert all(s == 0.01 for s in sleeps)  # Retry-After honored (already tiny)


def test_wp_429_forever_surfaces_honest_502(monkeypatch):
    monkeypatch.setattr(thailandnow, "_WP_429_BACKOFF_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))

    async def fake_sleep(s):
        pass

    monkeypatch.setattr(thailandnow.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="429 Too Many Requests")

    _mock_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(thailandnow._wp("GET", "/posts"))
    assert "429" in ei.value.detail and "WP GET /posts" in ei.value.detail


class _FakeMedia:
    """DELETE/GET media backend for the delete tests — records force-deletes."""

    def __init__(self, images):
        self.images = dict(images)  # id -> mime_type
        self.deleted: list[int] = []

    async def __call__(self, method, path, params=None, json_body=None):
        mid = int(path.strip("/").split("/")[-1])
        if method == "GET":
            if mid not in self.images:
                raise HTTPException(404, f"media {mid} not found")
            return {"id": mid, "mime_type": self.images[mid],
                    "source_url": f"{SITE}/m{mid}.png"}
        if method == "DELETE":
            if str((params or {}).get("force")) != "true":
                raise HTTPException(400, "does not support trashing")
            self.deleted.append(mid)
            return {"deleted": True, "id": mid}
        raise HTTPException(500, f"unexpected {method}")


def test_delete_media_guards_and_deletes(client, monkeypatch):
    fake = _FakeMedia({9: "image/png", 10: "video/mp4"})
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp", fake)

    # refuses non-image
    r = client.post("/api/thailandnow/seo/delete-media", json={"media_id": 10})
    assert r.status_code == 415
    # deletes image with force=true
    r2 = client.post("/api/thailandnow/seo/delete-media", json={"media_id": 9})
    assert r2.json()["ok"] is True and fake.deleted == [9]


def test_delete_media_bulk_paced_counts(client, monkeypatch):
    fake = _FakeMedia({9: "image/png", 11: "image/png", 12: "image/png"})
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp", fake)
    monkeypatch.setattr(thailandnow, "_opts", lambda: {"seo_bulk_delay_s": 0})
    r = client.post("/api/thailandnow/seo/delete-media-bulk",
                    json={"ids": [9, 11, 12, 99]})
    d = r.json()
    assert d["deleted"] == 3 and d["failed"] == 1
    assert d["error_sample"] and "not found" in d["error_sample"][0]
    assert fake.deleted == [9, 11, 12]


def test_apply_image_shrink_bulk_counts(client, fake_wp, monkeypatch):
    monkeypatch.setattr(thailandnow, "_opts", lambda: {"seo_bulk_delay_s": 0})
    fake_wp({11: f'<p><img src="{SITE}/big.jpg"></p>', 12: "<p>none</p>"})
    r = client.post("/api/thailandnow/seo/apply-image-shrink-bulk", json={"items": [
        {"post_id": 11, "old_src": f"{SITE}/big.jpg", "new_src": f"{SITE}/big-1024x576.jpg"},
        {"post_id": 12, "old_src": f"{SITE}/big.jpg", "new_src": f"{SITE}/big-1024x576.jpg"},
    ]})
    d = r.json()
    assert d["shrunk"] == 1 and d["noop"] == 1 and d["failed"] == 0
    assert d["total"] == 2


# ------------------------------------------------------------------- scopes ---

def test_content_cache_reuses_snapshot(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_all():
        calls["n"] += 1
        return [], [], [], [], set(), set()

    monkeypatch.setattr(thailandnow, "_seo_fetch_all", fake_fetch_all)
    first, cached1 = asyncio.run(thailandnow._seo_fetch_all_cached())
    second, cached2 = asyncio.run(thailandnow._seo_fetch_all_cached())
    assert calls["n"] == 1 and not cached1 and cached2 and first is second


def test_content_cache_ttl_expires(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_all():
        calls["n"] += 1
        return [], [], [], [], set(), set()

    monkeypatch.setattr(thailandnow, "_seo_fetch_all", fake_fetch_all)
    monkeypatch.setattr(thailandnow, "_opts", lambda: {"seo_content_cache_ttl_s": 0})
    asyncio.run(thailandnow._seo_fetch_all_cached())
    asyncio.run(thailandnow._seo_fetch_all_cached())
    assert calls["n"] == 2  # ttl 0 → always fresh


def test_flow_scope_sitemap_skips_content(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("fetch_all must not run in sitemap scope")

    async def fake_urls():
        return [f"{SITE}/a/", f"{SITE}/b/"], ""

    async def fake_hyg(urls):
        return {"checked": 2, "redirects": [], "noindex": [], "broken": [], "manual": [],
                "ok": [f"{SITE}/a/"], "challenge_loops": 0}

    monkeypatch.setattr(thailandnow, "_seo_fetch_all", boom)
    monkeypatch.setattr(thailandnow, "_seo_sitemap_urls", fake_urls)
    monkeypatch.setattr(thailandnow, "_seo_sitemap_hygiene", fake_hyg)
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp_site_host", lambda: "www.thailandnow.in.th")
    job = _flow_job()
    asyncio.run(thailandnow._flow_seo_health(job, scope="sitemap"))
    assert job.result["scope"] == "sitemap"
    assert job.result["sitemap"]["checked"] == 2
    assert "orphans" not in job.result


def test_flow_scope_images_no_probes(monkeypatch):
    probe_hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        probe_hits["n"] += 1
        return httpx.Response(200, text="<p>x</p>")

    async def fake_fetch_all():
        posts = [_rec(1, "/linker/", f'<p><img src="{SITE}/heavy.jpg"></p>')]
        return posts, [], [], [], set(), set()

    async def fake_oversized():
        return [{"id": 9, "source_url": f"{SITE}/heavy.jpg", "filesize": 3_000_000,
                 "shrink_to": f"{SITE}/heavy-1024x576.jpg"}], ""

    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp_site_host", lambda: "www.thailandnow.in.th")
    monkeypatch.setattr(thailandnow, "_seo_fetch_all", fake_fetch_all)
    monkeypatch.setattr(thailandnow, "_seo_oversized_media", fake_oversized)
    _mock_client(monkeypatch, handler)
    job = _flow_job()
    asyncio.run(thailandnow._flow_seo_health(job, scope="images"))
    res = job.result
    assert res["scope"] == "images"
    assert res["oversized_images"][0]["klass"] == "full-used"
    assert probe_hits["n"] == 0  # REST only — no probing at all
    assert "broken_external_links" not in res


def test_flow_scope_links_no_sitemap_section(monkeypatch):
    posts = [_rec(1, "/linker/", '<p><a href="/target/">t</a></p>'),
             _rec(2, "/target/", "<p>x</p>")]

    async def fake_fetch_all():
        return posts, [], [], [], set(), set()

    async def fake_chrome(extra, host):
        return set(), 0, 0

    async def boom(*a, **k):
        raise AssertionError("sitemap must not run in links scope")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>fine</body></html>")

    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    monkeypatch.setattr(thailandnow, "_wp_site_host", lambda: "www.thailandnow.in.th")
    monkeypatch.setattr(thailandnow, "_seo_fetch_all", fake_fetch_all)
    monkeypatch.setattr(thailandnow, "_seo_chrome_inbound", fake_chrome)
    monkeypatch.setattr(thailandnow, "_seo_sitemap_urls", boom)
    monkeypatch.setattr(thailandnow, "_seo_oversized_media", boom)
    _mock_client(monkeypatch, handler)
    job = _flow_job()
    asyncio.run(thailandnow._flow_seo_health(job, scope="links"))
    res = job.result
    assert res["scope"] == "links"
    assert "sitemap" not in res and "oversized_images" not in res
    assert {thailandnow._seo_path(o["link"]) for o in res["orphans"]} == {"/linker"}


def test_seo_challenge_detection():
    assert thailandnow._seo_challenge("\t <html><title>You are being redirected...</title>")
    assert thailandnow._seo_challenge("sucuri cloudproxy block page")
    assert not thailandnow._seo_challenge("<html><body>totally fine</body></html>")


def test_wp_falls_back_to_curl_on_challenge(monkeypatch):
    """httpx gets the interstitial (200 + challenge body) → _wp retries the call
    through the impersonated fallback instead of dying on non-JSON."""
    monkeypatch.setattr(thailandnow, "_CURL_OK", True)
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))
    via_curl = {"n": 0}

    async def fake_via_curl(method, target, params, json_body, auth):
        via_curl["n"] += 1
        return [{"id": 1}]

    monkeypatch.setattr(thailandnow, "_wp_via_curl", fake_via_curl)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="\t <html><title>You are being redirected...</title></html>")

    _mock_client(monkeypatch, handler)
    out = asyncio.run(thailandnow._wp("GET", "/posts", {"per_page": 1}))
    assert out == [{"id": 1}] and via_curl["n"] == 1


def test_wp_no_fallback_when_curl_missing(monkeypatch):
    """Without the optional dep the old degrade applies: non-JSON → honest 502."""
    monkeypatch.setattr(thailandnow, "_CURL_OK", False)
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: (SITE, "u", "p"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="\t <html><title>You are being redirected...</title></html>")

    _mock_client(monkeypatch, handler)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(thailandnow._wp("GET", "/posts"))
    assert "non-JSON" in ei.value.detail


def test_sitemap_hygiene_retries_challenge_as_chrome(monkeypatch):
    """403 from httpx + impersonation available → retried as Chrome and classified
    from the real response (ok bucket), not dumped into challenge loops."""
    monkeypatch.setattr(thailandnow, "_CURL_OK", True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("user-agent", "").startswith("curl"):
            pass  # curl_cffi sets its own UA — can't distinguish this way
        return httpx.Response(403, text="<html>You are being redirected...</html>")

    # the impersonated call goes through _curl_get — fake IT instead
    async def fake_curl_get(url, **kw):
        return 200, {"x-robots-tag": ""}, b"<html><body>clean</body></html>"

    monkeypatch.setattr(thailandnow, "_curl_get", fake_curl_get)
    _mock_client(monkeypatch, handler)
    h = asyncio.run(thailandnow._seo_sitemap_hygiene([f"{SITE}/page/"]))
    assert h["ok"] == [f"{SITE}/page/"] and h["challenge_loops"] == 0
