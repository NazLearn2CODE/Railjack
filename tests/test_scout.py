import asyncio
from app import thailandnow


def test_looks_like_url():
    assert thailandnow._looks_like_url("https://bangkokpost.com/thailand/general/1234") is True
    assert thailandnow._looks_like_url("http://example.com") is True
    assert thailandnow._looks_like_url("   https://example.com/article  ") is True
    assert thailandnow._looks_like_url("Thailand visa rules") is False
    assert thailandnow._looks_like_url(None) is False
    assert thailandnow._looks_like_url("") is False


def test_scout_dedup_by_domain():
    urls = [
        "https://bangkokpost.com/news/1",
        "https://www.bangkokpost.com/news/2",
        "https://nationthailand.com/news/1",
        "https://thailandnow.in.th/excluded/1",
    ]
    # by_domain=True: keeps one per domain and excludes excluded domains
    res = thailandnow._scout_dedup(urls, by_domain=True)
    assert len(res) == 2
    assert res[0] == "https://bangkokpost.com/news/1"
    assert res[1] == "https://nationthailand.com/news/1"


def test_scout_dedup_exact_url():
    urls = [
        "https://bangkokpost.com/news/1",
        "https://bangkokpost.com/news/2",
        "https://bangkokpost.com/news/1",
        "https://thailandnow.in.th/excluded/1",
    ]
    # by_domain=False: allows multiple URLs from same domain, dedups exact duplicates only
    res = thailandnow._scout_dedup(urls, by_domain=False)
    assert len(res) == 2
    assert res[0] == "https://bangkokpost.com/news/1"
    assert res[1] == "https://bangkokpost.com/news/2"


def test_wp_upload_media(monkeypatch):
    monkeypatch.setattr(thailandnow, "_wp_creds", lambda: ("https://example.com", "user", "pass"))

    post_calls = []

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/media/99":
            post_calls.append(json_body)
            return {"id": 99, "source_url": "https://example.com/uploaded.jpg", "link": "https://example.com/link"}
        return None

    monkeypatch.setattr(thailandnow, "_wp", mock_wp)

    class MockResponse:
        def __init__(self, status_code, content, headers=None, json_data=None):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {}
            self._json_data = json_data or {}
            self.text = "mock text"

        def json(self):
            return self._json_data

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None):
            return MockResponse(200, b"fake_image_bytes", {"content-type": "image/jpeg"})

        async def post(self, url, content=None, headers=None):
            return MockResponse(201, b"{}", json_data={"id": 99, "source_url": "https://example.com/uploaded.jpg"})

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    res = asyncio.run(thailandnow._wp_upload_media(
        image_url="https://images.pexels.com/photos/32710267/pexels-photo-32710267.jpeg",
        title="Pexels Test Image",
        alt_text="A scenic test view",
        caption="Source: pexels.com / Website",
    ))

    assert res["id"] == 99
    assert res["source_url"] == "https://example.com/uploaded.jpg"
    assert len(post_calls) == 1
    assert post_calls[0] == {
        "title": "Pexels Test Image",
        "alt_text": "A scenic test view",
        "caption": "Source: pexels.com / Website",
    }
