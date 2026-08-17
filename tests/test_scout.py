import asyncio
import json
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


def test_scout_terminal_report(tmp_path, monkeypatch):
    handoff_file = tmp_path / "latest.json"
    data = [
        {"title": "Story 1", "url": "https://example.com/1", "excerpt": "Excerpt summary 1", "date": "2026-07-27", "lang": "en", "source": "example.com"},
        {"title": "Story 2 (no url)", "url": "", "snippet": "No url row"},
        {"title": "Story 1 Duplicate", "url": "https://example.com/1", "snippet": "Dup url row"},
        {"title": "Story 3", "url": "https://example.com/3", "snippet": "Snippet 3"},
    ]
    handoff_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(thailandnow, "_SCOUT_HANDOFF", handoff_file)

    res = asyncio.run(thailandnow.scout_terminal_report())

    assert res["count"] == 2
    results = res["results"]
    assert len(results) == 2

    # Check mapping excerpt -> snippet for Story 1
    assert results[0]["url"] == "https://example.com/1"
    assert results[0]["snippet"] == "Excerpt summary 1"

    # Check url-less row dropped and duplicate url deduped
    assert results[1]["url"] == "https://example.com/3"
    assert results[1]["snippet"] == "Snippet 3"


def test_weekday_due_dates_within_month():
    # July 2026: Mon/Wed/Fri — first 3 should be within the first week
    dates = thailandnow._weekday_due_dates("202607", [0, 2, 4], 3)
    assert len(dates) == 3
    for d in dates:
        from datetime import datetime as _dt
        assert _dt.strptime(d, "%Y-%m-%d").weekday() in (0, 2, 4)
    assert dates == sorted(dates)  # ascending


def test_weekday_due_dates_thursdays():
    dates = thailandnow._weekday_due_dates("202607", [3], 4)
    assert len(dates) == 4
    from datetime import datetime as _dt
    for d in dates:
        assert _dt.strptime(d, "%Y-%m-%d").weekday() == 3


def test_weekday_due_dates_spills_into_next_month():
    # Ask for more Thursdays than a single month reliably has (5) to prove the
    # walk continues past month-end instead of raising/truncating.
    dates = thailandnow._weekday_due_dates("202607", [3], 6)
    assert len(dates) == 6
    from datetime import datetime as _dt
    for d in dates:
        assert _dt.strptime(d, "%Y-%m-%d").weekday() == 3
    assert dates == sorted(dates)


def test_fireside_filter_registry():
    rows = [
        {"topic": "Digital Nomad Visa (DTV)", "status": "done", "ep": "EP 10"},
        {"topic": "Crypto Tax Rules", "status": "revisitable", "ep": "EP 25"},
        {"topic": "Unverified Rumor", "status": "excluded", "ep": "EP 03"},
        {"topic": "Hospital Cost Breakdown", "status": "Done", "ep": "EP 40"},
        {"topic": "Condo Buying Traps", "status": "Revisitable", "ep": "EP 55"},
        {"topic": "", "status": "done", "ep": "EP 99"},
    ]
    done_topics, revisitable = thailandnow._filter_fireside_registry(rows)
    assert done_topics == ["Digital Nomad Visa (DTV)", "Unverified Rumor", "Hospital Cost Breakdown"]
    assert len(revisitable) == 2
    assert revisitable[0]["topic"] == "Crypto Tax Rules"
    assert revisitable[1]["topic"] == "Condo Buying Traps"


def test_fireside_registry_fetch(monkeypatch):
    async def mock_google_token():
        return "mock_google_token_xyz"

    monkeypatch.setattr(thailandnow, "_google_token", mock_google_token)

    sample_sheet_data = {
        "values": [
            ["VideoID", "Run", "EP", "Topic", "Status", "Co-host", "UploadDate", "Angle/Notes"],
            ["v123", "Run 1", "EP 01", "Bangkok Life", "done", "CoHostA", "2024-01-01", "Good intro"],
            ["v124", "Run 1", "EP 02", "Visa Pitfalls", "revisitable", "CoHostB", "2024-01-08", "Needs 2026 update"],
        ]
    }

    class MockResponse:
        def __init__(self, status_code, json_data):
            self.status_code = status_code
            self._json_data = json_data
            self.text = json.dumps(json_data)

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
            return MockResponse(200, sample_sheet_data)

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    res = asyncio.run(thailandnow._fireside_registry())
    assert len(res) == 2
    assert res[0]["video_id"] == "v123"
    assert res[0]["topic"] == "Bangkok Life"
    assert res[0]["status"] == "done"
    assert res[1]["topic"] == "Visa Pitfalls"
    assert res[1]["status"] == "revisitable"


def test_fireside_nid_resolution(tmp_path, monkeypatch):
    # 1. Options override
    monkeypatch.setattr(thailandnow, "_opts", lambda: {"fireside_notebook_id": "opt-nid-99"})
    assert asyncio.run(thailandnow._fireside_nid()) == "opt-nid-99"

    # 2. Sidecar file
    monkeypatch.setattr(thailandnow, "_opts", lambda: {})
    sidecar = tmp_path / "fireside_notebook.id"
    sidecar.write_text("sidecar-nid-42", encoding="utf-8")
    monkeypatch.setattr(thailandnow, "_fireside_nb_id_path", lambda: sidecar)
    assert asyncio.run(thailandnow._fireside_nid()) == "sidecar-nid-42"

    # 3. Discovery from _cached_notebooks
    sidecar.unlink()
    async def mock_cached_notebooks():
        return [
            {"id": "other-1", "title": "Thailand NOW Events"},
            {"id": "fireside-nb-77", "title": "The Fireside Episodes Archive"},
        ]
    monkeypatch.setattr(thailandnow, "_cached_notebooks", mock_cached_notebooks)
    assert asyncio.run(thailandnow._fireside_nid()) == "fireside-nb-77"

    # 4. None when missing
    async def mock_cached_empty():
        return [{"id": "other-1", "title": "Unrelated"}]
    monkeypatch.setattr(thailandnow, "_cached_notebooks", mock_cached_empty)
    assert asyncio.run(thailandnow._fireside_nid()) is None

    # 5. _fireside_ensure raises HTTPException(424)
    from fastapi import HTTPException
    try:
        asyncio.run(thailandnow._fireside_ensure())
        assert False, "Should have raised HTTPException 424"
    except HTTPException as e:
        assert e.status_code == 424


def test_fireside_source_flow_notebook_mode(monkeypatch):
    async def mock_registry():
        return [{"topic": "Past Visa Ep", "status": "done", "ep": "EP 01"}]

    async def mock_ensure():
        return "nb-corpus-01"

    async def mock_run_cli(argv, timeout=60, parse=True):
        if "source" in argv and "list" in argv:
            return {"sources": [{"id": "src-1", "url": "https://bangkokpost.com/news/1"}]}
        if "ask" in argv or "query" in argv:
            return {
                "answer": "Here are fresh episode angles based on the corpus...",
                "references": [{"source_id": "src-1"}],
            }
        return {}

    async def mock_zai_message(prompt, max_tokens=None, system=None, model=None, timeout=None):
        return json.dumps({
            "topics": [
                {
                    "title": "Digital Nomad Tax Realities in 2026",
                    "angle": "1. What is the tax rate? 2. How to comply easily?",
                    "ep_adjacent": ["EP 01"],
                    "source_urls": ["https://bangkokpost.com/news/1"],
                    "if_like_a_try_b": "If you liked EP 01, try this.",
                    "visual_style": "Table graphic",
                    "why_fresh": "Brand new 2026 rule changes",
                    "revisit_candidate": False,
                }
            ]
        })

    monkeypatch.setattr(thailandnow, "_fireside_registry", mock_registry)
    monkeypatch.setattr(thailandnow, "_fireside_nid", mock_ensure)
    monkeypatch.setattr(thailandnow, "_run_cli", mock_run_cli)
    monkeypatch.setattr(thailandnow, "zai_message", mock_zai_message)

    job = thailandnow.TnJob(id="test1", kind="fireside-source", label="test")
    asyncio.run(thailandnow._flow_fireside_source(job, seed="Digital Nomad Tax", category=None))

    assert job.result is not None
    assert job.result["mode"] == "notebook"
    assert job.result["notebook_id"] == "nb-corpus-01"
    assert len(job.result["topics"]) == 1
    assert job.result["topics"][0]["title"] == "Digital Nomad Tax Realities in 2026"
    assert job.result["topics"][0]["source_urls"] == ["https://bangkokpost.com/news/1"]


def test_fireside_source_flow_ask_thin_web_fallback(monkeypatch):
    async def mock_registry():
        return []

    async def mock_ensure():
        return "nb-corpus-02"

    async def mock_run_cli(argv, timeout=60, parse=True):
        if "source" in argv and "list" in argv:
            return {"sources": []}
        if "ask" in argv or "query" in argv:
            return {"answer": "", "references": []}  # THIN/EMPTY answer
        return {}

    async def mock_jina_read(url):
        return "# Thailand Visa Policy\nPublished Time: 2026-08-01\nNew rules for foreigners."

    monkeypatch.setattr(thailandnow, "_fireside_registry", mock_registry)
    monkeypatch.setattr(thailandnow, "_fireside_nid", mock_ensure)
    monkeypatch.setattr(thailandnow, "_run_cli", mock_run_cli)
    monkeypatch.setattr(thailandnow, "_jina_read", mock_jina_read)
    monkeypatch.setattr(thailandnow, "_brave_urls", lambda q: asyncio.sleep(0, result=["https://nationthailand.com/news/2"]))
    monkeypatch.setattr(thailandnow, "_gnews_urls", lambda q: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(thailandnow, "_parse_ddg", lambda md: [])

    async def mock_zai_message(prompt, max_tokens=None, system=None, model=None, timeout=None):
        return json.dumps({
            "topics": [
                {
                    "title": "Fallback Web Sourced Topic",
                    "angle": "1. What is new? 2. How to apply?",
                    "ep_adjacent": [],
                    "source_urls": ["https://nationthailand.com/news/2"],
                    "if_like_a_try_b": "If you like travel, try this.",
                    "visual_style": "Document callout",
                    "why_fresh": "Fresh web news",
                    "revisit_candidate": False,
                }
            ]
        })

    monkeypatch.setattr(thailandnow, "zai_message", mock_zai_message)

    job = thailandnow.TnJob(id="test2", kind="fireside-source", label="test fallback")
    asyncio.run(thailandnow._flow_fireside_source(job, seed="Visa Update", category="expat-policy"))

    assert job.result is not None
    assert job.result["mode"] == "web-fallback"
    assert job.result["notebook_id"] == "nb-corpus-02"
    assert len(job.result["topics"]) == 1
    assert job.result["topics"][0]["title"] == "Fallback Web Sourced Topic"


def test_fireside_edit_notes_lenient_parsing():
    sample_edit_notes_raw = """
    Here are Ben's editorial notes on the draft script:
    ```json
    {
      "overall": "Strong hook and clear framing, but second half needs tighter pacing.",
      "strengths": [
        "Natural two-host banter in the opening",
        "Clear explanation of the visa fee"
      ],
      "fixes": [
        {
          "anchor": "You never need to show financial proof",
          "note": "Factually incorrect — clarify that 500k THB is required.",
          "severity": "must"
        },
        {
          "anchor": "Let's move on to the next thing",
          "note": "Add a proper chapter transition card.",
          "severity": "should"
        },
        {
          "anchor": "It is super cool",
          "note": "Slightly too colloquial, tighten phrasing.",
          "severity": "nit"
        }
      ],
      "structure_notes": "Break the second segment into two distinct chapters.",
      "voice_notes": "Ensure co-host has an equal share of conversational prompts.",
      "coverage_check": ""
    }
    ```
    Good work overall.
    """
    parsed = thailandnow._parse_json_lenient(sample_edit_notes_raw)
    assert isinstance(parsed, dict)
    assert "overall" in parsed
    assert len(parsed["strengths"]) == 2
    assert len(parsed["fixes"]) == 3
    assert parsed["fixes"][0]["severity"] == "must"
    assert parsed["fixes"][1]["severity"] == "should"
    assert parsed["fixes"][2]["severity"] == "nit"
    assert parsed["structure_notes"] == "Break the second segment into two distinct chapters."


def test_fireside_edit_flow(monkeypatch):
    async def mock_zai_message(prompt, max_tokens=None, system=None, model=None, timeout=None):
        return json.dumps({
            "overall": "Good draft.",
            "strengths": ["Clear intro"],
            "fixes": [],
            "structure_notes": "Well structured",
            "voice_notes": "Good tone",
            "coverage_check": "",
        })

    monkeypatch.setattr(thailandnow, "zai_message", mock_zai_message)

    # 1. Direct draft
    res = asyncio.run(thailandnow._fireside_edit(draft="Ben: Welcome back to The Fireside.\nCo-host: Today we discuss visas.", check_coverage=False))
    assert res["mode"] == "direct"
    assert res["notes"]["overall"] == "Good draft."

    # 2. Coverage check enabled
    async def mock_fireside_nid():
        return "nb-coverage-1"

    async def mock_run_cli(argv, timeout=60, parse=True):
        return {"answer": "Covered in EP 14 (Run 1) and EP 38 (Run 2)."}

    monkeypatch.setattr(thailandnow, "_fireside_nid", mock_fireside_nid)
    monkeypatch.setattr(thailandnow, "_run_cli", mock_run_cli)

    res_cov = asyncio.run(thailandnow._fireside_edit(draft="Ben: Welcome back.\nCo-host: Today we discuss DTV.", check_coverage=True))
    assert res_cov["mode"] == "direct"
    assert "EP 14" in res_cov["notes"]["coverage_check"]

    # 3. Degraded URL read
    async def mock_jina_fail(url):
        raise Exception("Fetch failed")

    monkeypatch.setattr(thailandnow, "_jina_read", mock_jina_fail)
    res_deg = asyncio.run(thailandnow._fireside_edit(url="https://docs.google.com/document/d/invalid/edit"))
    assert res_deg["mode"] == "degraded"
    assert "couldn't read the URL" in res_deg["error"]



def test_parse_reddit_posts_real_shape():
    # verbatim opencli reddit search -f json shape (2026-08-17)
    rows = [{"title": "Visa thread", "url": "https://www.reddit.com/r/Thailand/comments/1/x/",
             "subreddit": "r/Thailand", "created_utc": 1786754473,
             "selftext": "long discussion"}]
    arts = thailandnow._parse_reddit_posts(json.dumps(rows))
    assert arts == [{"title": "Visa thread", "url": rows[0]["url"],
                     "snippet": "long discussion", "date": "2026-08-15",
                     "lang": "en", "source": "reddit.com/r/Thailand"}]
    assert thailandnow._parse_reddit_posts("not json") == []
    assert thailandnow._parse_reddit_posts('{"data": []}') == []  # non-list → []


def test_parse_twitter_posts_opencli_shape_recency_and_short_drop():
    # opencli twitter tweets -f json: created_at / author as plain string
    rows = [
        {"id": "111", "text": "x" * 60, "created_at": "Mon Aug 17 08:00:00 +0000 2026",
         "author": "ThaiPBSWorld"},
        {"id": "222", "text": "short",  # dropped: <40 chars
         "created_at": "Mon Aug 17 08:00:00 +0000 2026", "author": "ThaiPBSWorld"},
        {"id": "333", "text": "y" * 60,  # dropped: too old
         "created_at": "Mon Jan 01 08:00:00 +0000 2026", "author": "ThaiPBSWorld"},
    ]
    arts = thailandnow._parse_twitter_posts(json.dumps(rows), "fallback", days=7)
    assert len(arts) == 1
    assert arts[0]["source"] == "x.com/ThaiPBSWorld"
    assert arts[0]["url"] == "https://x.com/ThaiPBSWorld/status/111"
