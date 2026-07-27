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
