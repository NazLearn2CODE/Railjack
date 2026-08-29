"""Tests for THAILAND NOW WordPress OP pipeline endpoints:
  GET  /api/thailandnow/events/to-publish
  POST /api/thailandnow/events/analyze-card
  POST /api/thailandnow/events/publish-from-card
"""

import pytest
from datetime import datetime
from fastapi import HTTPException

from app.thailandnow import analyze_card, events_to_publish


@pytest.fixture(autouse=True)
def _stub_sheets_ops(monkeypatch):
    """Ensure no real Google Sheets HTTP calls are made during tests."""
    async def mock_noop(*args, **kwargs):
        return None

    async def mock_read_all(*args, **kwargs):
        return []

    async def mock_find_row(*args, **kwargs):
        return None

    monkeypatch.setattr("app.thailandnow._sheet_update_cell", mock_noop)
    monkeypatch.setattr("app.thailandnow._sheet_update_range", mock_noop)
    monkeypatch.setattr("app.thailandnow._sheet_append_rows", mock_noop)
    monkeypatch.setattr("app.thailandnow._sheet_read_all", mock_read_all)
    monkeypatch.setattr("app.thailandnow._ensure_pipeline_tab", mock_noop)
    monkeypatch.setattr("app.thailandnow._pipeline_find_row", mock_find_row)


@pytest.mark.anyio
async def test_events_to_publish_happy_path(monkeypatch):
    async def mock_trello(method, path, params=None, body=None):
        assert method == "GET"
        assert path == "/lists/685686f5a5d5ec7d657af3c6/cards"
        assert params == {"fields": "id,name"}
        return [
            {"id": "card_101", "name": "Article | AUG #02"},
            {"id": "card_102", "name": "Event | AUG #03"},
        ]

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)

    res = await events_to_publish()
    assert res == {
        "cards": [
            {"id": "card_101", "name": "Article | AUG #02"},
            {"id": "card_102", "name": "Event | AUG #03"},
        ]
    }


@pytest.mark.anyio
async def test_analyze_card_happy_path_attachment(monkeypatch):
    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_101":
            return {"id": "card_101", "name": "Article | AUG #02", "desc": "Some description"}
        if path == "/cards/card_101/attachments":
            return [
                {
                    "name": "Doc Attachment",
                    "url": "https://docs.google.com/document/d/DOC_ABC_123/edit?usp=sharing",
                }
            ]
        return {}

    async def mock_google_token():
        return "mock_google_token_val"

    async def mock_drive_read_doc(token, doc_id):
        assert token == "mock_google_token_val"
        assert doc_id == "DOC_ABC_123"
        return "Full text content of Google Doc AUG #02."

    async def mock_generate_event_seo(title, body_text, category="Events"):
        assert title == "AUG #02"  # Prefix stripped!
        assert body_text == "Full text content of Google Doc AUG #02."
        assert category == "Articles"  # Card name starts with "Article |" → auto-detected
        seo = {
            "keyphrases": ["k1", "k2", "k3", "k4", "k5"],
            "metas": ["m1", "m2", "m3", "m4", "m5"],
            "hashtags": "#tag1 #tag2",
            "ai_a": "Summary A text",
            "ai_b": ["Takeaway 1", "Takeaway 2"],
        }
        return seo, "gemini-3.6-flash (agy)"

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)

    res = await analyze_card({"card_id": "card_101"})

    assert res["card_id"] == "card_101"
    assert res["title"] == "AUG #02"
    assert res["doc_text"] == "Full text content of Google Doc AUG #02."
    assert res["seo_model"] == "gemini-3.6-flash (agy)"
    assert len(res["seo"]["keyphrases"]) == 5
    assert len(res["seo"]["metas"]) == 5
    assert res["seo"]["hashtags"] == "#tag1 #tag2"
    assert res["seo"]["ai_a"] == "Summary A text"
    assert len(res["seo"]["ai_b"]) == 2


@pytest.mark.anyio
async def test_analyze_card_happy_path_desc_fallback(monkeypatch):
    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_102":
            return {
                "id": "card_102",
                "name": "Event | AUG #03",
                "desc": "Check doc: https://docs.google.com/document/d/DOC_XYZ_789/edit",
            }
        if path == "/cards/card_102/attachments":
            return []  # No attachments
        return {}

    async def mock_google_token():
        return "mock_google_token_val"

    async def mock_drive_read_doc(token, doc_id):
        assert doc_id == "DOC_XYZ_789"
        return "Fallback desc doc text."

    async def mock_generate_event_seo(title, body_text, category="Events"):
        assert title == "AUG #03"
        return {"keyphrases": [], "metas": [], "hashtags": "", "ai_a": "", "ai_b": []}, "glm-5"

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)

    res = await analyze_card({"card_id": "card_102"})
    assert res["title"] == "AUG #03"
    assert res["doc_text"] == "Fallback desc doc text."
    assert res["seo_model"] == "glm-5"


@pytest.mark.anyio
async def test_analyze_card_missing_card_id():
    with pytest.raises(HTTPException) as exc_info:
        await analyze_card({})
    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_analyze_card_no_doc_found_404(monkeypatch):
    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_103":
            return {"id": "card_103", "name": "Card Without Doc", "desc": "No links here"}
        if path == "/cards/card_103/attachments":
            return [{"name": "Image", "url": "https://example.com/pic.jpg"}]
        return {}

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)

    with pytest.raises(HTTPException) as exc_info:
        await analyze_card({"card_id": "card_103"})
    assert exc_info.value.status_code == 404
    assert "No Google Doc" in exc_info.value.detail


@pytest.mark.anyio
async def test_publish_event_from_card_happy_path(monkeypatch):
    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_201":
            return {"id": "card_201", "name": "Event | AUG #02", "desc": "Card desc", "due": "2026-08-15T00:00:00.000Z"}
        if path == "/cards/card_201/attachments":
            return [{"name": "Doc", "url": "https://docs.google.com/document/d/DOC_AUG02/edit"}]
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        assert doc_id == "DOC_AUG02"
        return "Plain text of AUG #02 event."

    async def mock_drive_read_doc_html(token, doc_id):
        assert doc_id == "DOC_AUG02"
        return "<h1>AUG #02 Event</h1><p>Join AUG #02 event in Bangkok!</p>"

    async def mock_generate_event_seo(title, body_text, category="Events"):
        assert title == "AUG #02 Event 2026"  # title from the doc headline + event year
        assert body_text == "Plain text of AUG #02 event."
        return {
            "keyphrases": ["AUG #02 event", "Bangkok event", "k3", "k4", "k5"],
            "metas": ["Join AUG #02 event in Bangkok!", "m2", "m3", "m4", "m5"],
            "hashtags": "#AUG02 #Bangkok",
            "ai_a": "Summary text",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    captured = {}

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            captured["body"] = json_body
            return {"id": 8888, "link": "https://www.thailandnow.in.th/event/aug-02"}
        return {}

    from app.thailandnow import publish_event_from_card

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)

    res = await publish_event_from_card({"card_id": "card_201"})

    assert res["wp_id"] == 8888
    assert res["status"] == "draft"
    assert res["seo_model"] == "gemini-3.6-flash (agy)"
    assert res["images_uploaded"] == 0
    # title from the doc headline + event year
    assert captured["body"]["title"] == "AUG #02 Event 2026"
    content = captured["body"]["content"]
    assert 'wp-block-heading">Key Takeaways</h2>' in content   # Key Takeaways seated near top
    assert "AUG #02 Event</h2>" not in content                  # title heading stripped from body
    assert "ai-summary" not in content                          # no AI Summary injection
    assert 'class="hashtags"' not in content                    # no hashtag line
    # SEO payload trimmed to Focus keyphrase + Meta Description + Key Takeaways
    assert res["seo"]["focus_keyphrase"] == "AUG #02 event"
    assert res["seo"]["meta_description"] == "Join AUG #02 event in Bangkok!"
    assert res["seo"]["key_takeaways"] == ["Takeaway 1"]
    assert res["registry"] == "draft-logged"


@pytest.mark.anyio
async def test_publish_event_from_card_missing_card_id():
    from app.thailandnow import publish_event_from_card
    with pytest.raises(HTTPException) as exc_info:
        await publish_event_from_card({})
    assert exc_info.value.status_code == 400


def test_extract_doc_title_heading_and_no_heading():
    from app.thailandnow import _extract_doc_title

    # With h1 or h2 heading
    assert _extract_doc_title("<h1>Event Headline</h1><p>Body</p>", "Facebook\nBody") == "Event Headline"
    assert _extract_doc_title("<h2>Subheadline Event</h2>", "Draft text") == "Subheadline Event"

    # Heading-less doc (e.g. plain text / social paste doc with empty HTML) -> returns ""
    assert _extract_doc_title("", "Facebook\n9th Sun-Dried Squid Festival") == ""
    assert _extract_doc_title("<html><body><p>No headings here</p></body></html>", "Some text") == ""


def test_convert_text_to_gutenberg_social_label():
    from app.thailandnow import _convert_text_to_gutenberg

    text = """Facebook

9th Sun-Dried Squid & Mini Hot Air Balloon Festival

Pak Bara Beach, La-ngu District, Satun"""

    gtb = _convert_text_to_gutenberg(text)
    assert "Facebook" not in gtb
    assert '<p class="wp-block-paragraph">9th Sun-Dried Squid & Mini Hot Air Balloon Festival</p>' in gtb
    assert '<p class="wp-block-paragraph">Pak Bara Beach, La-ngu District, Satun</p>' in gtb


@pytest.mark.anyio
async def test_publish_event_from_card_text_fallback(monkeypatch):
    from app.thailandnow import publish_event_from_card

    curr_year = datetime.now().year

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_squid":
            return {
                "id": "card_squid",
                "name": "Event | 9th Sun-Dried Squid & Mini Hot Air Balloon Festival",
                "desc": "Doc: https://docs.google.com/document/d/DOC_SQUID/edit",
            }
        if path == "/cards/card_squid/attachments":
            return []
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        assert doc_id == "DOC_SQUID"
        return "Facebook\n\n9th Sun-Dried Squid & Mini Hot Air Balloon Festival\n\nPak Bara Beach"

    async def mock_drive_read_doc_html(token, doc_id):
        assert doc_id == "DOC_SQUID"
        return "<html><body></body></html>"  # Empty/minimal HTML export

    async def mock_generate_event_seo(title, body_text, category="Events"):
        assert title == f"9th Sun-Dried Squid & Mini Hot Air Balloon Festival {curr_year}"
        return {
            "keyphrases": ["Sun-Dried Squid Festival", "k2", "k3", "k4", "k5"],
            "metas": ["9th Sun-Dried Squid Festival in Satun.", "m2", "m3", "m4", "m5"],
            "hashtags": "#Squid #Satun",
            "ai_a": "Summary",
            "ai_b": ["Squid Takeaway"],
        }, "gemini-3.6-flash (agy)"

    captured = {}

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            captured["body"] = json_body
            return {"id": 9999, "link": "https://www.thailandnow.in.th/event/squid"}
        return {}

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)

    res = await publish_event_from_card({"card_id": "card_squid"})
    assert res["wp_id"] == 9999
    assert captured["body"]["title"] == f"9th Sun-Dried Squid & Mini Hot Air Balloon Festival {curr_year}"
    content = captured["body"]["content"]
    assert "Facebook" not in content
    assert '<p class="wp-block-paragraph">9th Sun-Dried Squid & Mini Hot Air Balloon Festival</p>' in content
    assert 'wp-block-heading">Key Takeaways</h2>' in content


@pytest.mark.anyio
async def test_publish_event_from_card_year_from_due(monkeypatch):
    """(a) card due='2026-08-15T...' -> title ends with '2026'"""
    from app.thailandnow import publish_event_from_card

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_due":
            assert params == {"fields": "name,desc,due"}
            return {
                "id": "card_due",
                "name": "Event | Pattaya Music Festival",
                "desc": "Doc: https://docs.google.com/document/d/DOC_DUE/edit",
                "due": "2026-08-15T18:00:00.000Z",
            }
        if path == "/cards/card_due/attachments":
            return []
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        return "Pattaya Music Festival details"

    async def mock_drive_read_doc_html(token, doc_id):
        return "<p>Pattaya Music Festival details</p>"

    async def mock_generate_event_seo(title, body_text, category="Events"):
        assert title.endswith("2026")
        return {
            "keyphrases": ["Pattaya Music Festival 2026"],
            "metas": ["Meta description"],
            "hashtags": "#Pattaya",
            "ai_a": "Summary",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    captured = {}

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            captured["body"] = json_body
            return {"id": 1001, "link": "https://www.thailandnow.in.th/event/pattaya-2026"}
        return {}

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)

    res = await publish_event_from_card({"card_id": "card_due"})
    assert res["wp_id"] == 1001
    assert captured["body"]["title"] == "Pattaya Music Festival 2026"
    assert captured["body"]["title"].endswith("2026")


@pytest.mark.anyio
async def test_publish_event_from_card_year_fallback_current_year(monkeypatch):
    """(b) card due=None -> title ends with str(current year)"""
    from app.thailandnow import publish_event_from_card

    current_year = str(datetime.now().year)

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_nodue":
            assert params == {"fields": "name,desc,due"}
            return {
                "id": "card_nodue",
                "name": "Event | Chiang Mai Lantern Festival",
                "desc": "Doc: https://docs.google.com/document/d/DOC_NODUE/edit",
                "due": None,
            }
        if path == "/cards/card_nodue/attachments":
            return []
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        return "Lantern festival details"

    async def mock_drive_read_doc_html(token, doc_id):
        return "<p>Lantern festival details</p>"

    async def mock_generate_event_seo(title, body_text, category="Events"):
        assert title.endswith(current_year)
        return {
            "keyphrases": [f"Chiang Mai Lantern Festival {current_year}"],
            "metas": ["Meta description"],
            "hashtags": "#Lantern",
            "ai_a": "Summary",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    captured = {}

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            captured["body"] = json_body
            return {"id": 1002, "link": "https://www.thailandnow.in.th/event/lantern"}
        return {}

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)

    res = await publish_event_from_card({"card_id": "card_nodue"})
    assert res["wp_id"] == 1002
    assert captured["body"]["title"] == f"Chiang Mai Lantern Festival {current_year}"
    assert captured["body"]["title"].endswith(current_year)


@pytest.mark.anyio
async def test_publish_event_from_card_year_already_in_title_no_double_append(monkeypatch):
    """(c) a title already ending in a year (e.g. doc heading 'Foo 2026') -> unchanged (no double append)"""
    from app.thailandnow import publish_event_from_card

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_with_year":
            assert params == {"fields": "name,desc,due"}
            return {
                "id": "card_with_year",
                "name": "Event | Songkran Water Festival",
                "desc": "Doc: https://docs.google.com/document/d/DOC_WITH_YEAR/edit",
                "due": "2026-04-13T09:00:00.000Z",
            }
        if path == "/cards/card_with_year/attachments":
            return []
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        return "Songkran Water Festival 2026 plain text."

    async def mock_drive_read_doc_html(token, doc_id):
        return "<h1>Songkran Water Festival 2026</h1><p>Celebrate Songkran Water Festival 2026!</p>"

    async def mock_generate_event_seo(title, body_text, category="Events"):
        assert title == "Songkran Water Festival 2026"
        return {
            "keyphrases": ["Songkran Water Festival 2026"],
            "metas": ["Celebrate Songkran 2026"],
            "hashtags": "#Songkran2026",
            "ai_a": "Summary",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    captured = {}

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            captured["body"] = json_body
            return {"id": 1003, "link": "https://www.thailandnow.in.th/event/songkran-2026"}
        return {}

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)

    res = await publish_event_from_card({"card_id": "card_with_year"})
    assert res["wp_id"] == 1003
    assert captured["body"]["title"] == "Songkran Water Festival 2026"
    assert not captured["body"]["title"].endswith("2026 2026")


def test_parse_event_dates():
    from app.thailandnow import _parse_event_dates

    # 1. '7 - 9 August, 2026'
    assert _parse_event_dates("7 - 9 August, 2026") == ("2026-08-07", "2026-08-09")
    # 2. '7 to 9 August 2026'
    assert _parse_event_dates("7 to 9 August 2026") == ("2026-08-07", "2026-08-09")
    # 3. 'August 7 - 9, 2026'
    assert _parse_event_dates("August 7 - 9, 2026") == ("2026-08-07", "2026-08-09")
    # 4. 'July 30 - August 2, 2026'
    assert _parse_event_dates("July 30 - August 2, 2026") == ("2026-07-30", "2026-08-02")
    # 5. Single date 'August 7, 2026'
    assert _parse_event_dates("August 7, 2026") == ("2026-08-07", "2026-08-07")
    # 6. '7th - 9th August 2026'
    assert _parse_event_dates("7th - 9th August 2026") == ("2026-08-07", "2026-08-09")


def test_extract_google_doc_data_and_build_gutenberg():
    from app.thailandnow import _extract_google_doc_data, _build_gutenberg_from_doc_ast

    doc_ast = {
        "body": {
            "content": [
                # Social media guide to be discarded
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Facebook Post\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Instagram Post\n"}}]}},
                # Title
                {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_1"}, "elements": [{"textRun": {"content": "9th Sun-Dried Squid Festival\n"}}]}},
                # Location
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Pak Nam Pran, Prachuap Khiri Khan\n"}}]}},
                # Date Range
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "7 - 9 August, 2026\n"}}]}},
                # Intro paragraph 1
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Intro paragraph 1 content.\n"}}]}},
                # Intro paragraph 2
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Intro paragraph 2 content.\n"}}]}},
                # Heading 2
                {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_2"}, "elements": [{"textRun": {"content": "Squid Fishing & Food Stalls\n"}}]}},
                # Body paragraph
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Detail about food stalls.\n"}}]}},
            ]
        },
        "inlineObjects": {}
    }

    parsed = _extract_google_doc_data(doc_ast, card_name="Event | AUG #02", default_year=2026, append_year=True)
    assert parsed["title"] == "9th Sun-Dried Squid Festival 2026"
    assert parsed["location"] == "Pak Nam Pran, Prachuap Khiri Khan"
    assert parsed["dates_raw"] == "7 - 9 August, 2026"
    assert parsed["start_date"] == "2026-08-07"
    assert parsed["end_date"] == "2026-08-09"

    takeaways = ["Fresh seafood galore", "Live cultural shows"]
    gutenberg = _build_gutenberg_from_doc_ast(parsed["body_content"], {}, takeaways, parsed["content_start_idx"])

    # Verify structure:
    # 1. Group block for Key Takeaways
    assert '<!-- wp:group {"style":{"color":{"background":"#efefef"}},"layout":{"type":"constrained"}} -->' in gutenberg
    assert '<h2 id="h-key-takeaways" class="wp-block-heading">Key Takeaways</h2>' in gutenberg
    assert "<!-- wp:list -->" in gutenberg
    assert "<li>Fresh seafood galore</li>" in gutenberg
    # 2. Subheading 2
    assert '<!-- wp:heading {"anchor":"h-squid-fishing-food-stalls"} -->' in gutenberg
    assert '<h2 id="h-squid-fishing-food-stalls" class="wp-block-heading"><strong>Squid Fishing &amp; Food Stalls</strong></h2>' in gutenberg
    # 3. Intro paragraphs
    assert "<p>Intro paragraph 1 content.</p>" in gutenberg
    assert "<p>Intro paragraph 2 content.</p>" in gutenberg


def test_extract_google_doc_data_article_mode():
    """Articles skip location/date rows — content starts immediately after H1."""
    from app.thailandnow import _extract_google_doc_data

    doc_ast = {
        "body": {
            "content": [
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Editor note\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_1"}, "elements": [{"textRun": {"content": "Thai PM wraps 1st Indonesia visit\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Thai Prime Minister Anutin concluded a visit.\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "The trip ran August 3rd to 4th.\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_2"}, "elements": [{"textRun": {"content": "Economic agenda\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Trade talks progressed.\n"}}]}},
            ]
        },
        "inlineObjects": {}
    }

    parsed = _extract_google_doc_data(
        doc_ast, card_name="Article | AUG #02", default_year=2026, append_year=False, is_article=True
    )
    assert parsed["title"] == "Thai PM wraps 1st Indonesia visit"
    # No location or dates for articles
    assert parsed["location"] == ""
    assert parsed["dates_raw"] == ""
    assert parsed["start_date"] == ""
    assert parsed["end_date"] == ""
    # Intro para 1 is in body
    assert "Thai Prime Minister Anutin" in parsed["clean_body_text"]


def test_build_gutenberg_from_doc_ast_article_three_enters():
    """Articles get 3 empty-para spacers directly under each H2."""
    from app.thailandnow import _extract_google_doc_data, _build_gutenberg_from_doc_ast

    doc_ast = {
        "body": {
            "content": [
                {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_1"}, "elements": [{"textRun": {"content": "Test Article Title\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Intro para 1.\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Intro para 2.\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_2"}, "elements": [{"textRun": {"content": "Section One\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Body text under section one.\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_2"}, "elements": [{"textRun": {"content": "Section Two\n"}}]}},
                {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "elements": [{"textRun": {"content": "Body text under section two.\n"}}]}},
            ]
        },
        "inlineObjects": {}
    }

    parsed = _extract_google_doc_data(doc_ast, is_article=True)
    takeaways = ["Key point one", "Key point two"]
    gutenberg = _build_gutenberg_from_doc_ast(
        parsed["body_content"], {}, takeaways, parsed["content_start_idx"], is_article=True
    )

    # Key Takeaways group block present
    assert '<!-- wp:group {"style":{"color":{"background":"#efefef"}}' in gutenberg
    assert '<h2 id="h-key-takeaways"' in gutenberg
    assert "<li>Key point one</li>" in gutenberg

    empty_para = "<!-- wp:paragraph -->\n<p></p>\n<!-- /wp:paragraph -->"

    # Section One heading followed by 3 empty paras before body text
    pos = gutenberg.find('id="h-section-one"')
    assert pos != -1
    section_chunk = gutenberg[pos:gutenberg.find("<p>Body text under section one", pos)]
    count = section_chunk.count(empty_para)
    assert count == 3, f"Expected 3 empty-para spacers after H2, got {count}"

    # Section Two also gets 3 spacers
    pos2 = gutenberg.find('id="h-section-two"')
    assert pos2 != -1
    section2_chunk = gutenberg[pos2:gutenberg.find("<p>Body text under section two", pos2)]
    count2 = section2_chunk.count(empty_para)
    assert count2 == 3, f"Expected 3 empty-para spacers after second H2, got {count2}"

    # No image blocks in article mode
    assert "wp:image" not in gutenberg


@pytest.mark.anyio
async def test_create_event_doc_hook_b_pipeline_logged(monkeypatch):
    from app.thailandnow import create_event_doc, COVERED_OURS_SHEET

    captured_appends = []

    async def mock_provision(payload):
        return {
            "desk_id": "tian",
            "count": 1,
            "yyyymm": "202608",
            "items": [
                {
                    "nn": "01",
                    "doc_name": '[202608] [EN] "Bangkok Art Fest"',
                    "doc_url": "https://docs.google.com/document/d/DOC_BAF",
                    "card_name": "Event | Bangkok Art Fest",
                    "card_url": "https://trello.com/c/CARD_BAF",
                }
            ],
        }

    async def mock_ensure_pipeline():
        pass

    async def mock_sheet_append_rows(sheet_id, tab, rows):
        captured_appends.append((sheet_id, tab, rows))

    monkeypatch.setattr("app.thailandnow.provision", mock_provision)
    monkeypatch.setattr("app.thailandnow._ensure_pipeline_tab", mock_ensure_pipeline)
    monkeypatch.setattr("app.thailandnow._sheet_append_rows", mock_sheet_append_rows)

    payload = {
        "event": {
            "title": "Bangkok Art Fest",
            "url": "https://artfest.bkk/2026",
            "start_date": "2026-08-20",
        },
        "bundle_text": "Bundle content",
    }
    res = await create_event_doc(payload)

    assert res["registry"] == "pipeline-logged"
    assert len(captured_appends) == 1
    sheet_id, tab, rows = captured_appends[0]
    assert sheet_id == COVERED_OURS_SHEET
    assert tab == "Pipeline"
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == "Bangkok Art Fest"
    assert row[2] == "bangkokartfest"
    assert row[3] == "https://trello.com/c/CARD_BAF"
    assert row[4] == "https://docs.google.com/document/d/DOC_BAF"
    assert row[5] == "PIPELINE"


@pytest.mark.anyio
async def test_publish_event_from_card_hook_a_flips_existing_row(monkeypatch):
    from app.thailandnow import publish_event_from_card, COVERED_OURS_SHEET

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_flip":
            return {"id": "card_flip", "name": "Event | Chiang Mai Design Week", "desc": "Card desc"}
        if path == "/cards/card_flip/attachments":
            return [{"name": "Doc", "url": "https://docs.google.com/document/d/DOC_CMDW/edit"}]
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        return "Chiang Mai Design Week plain text"

    async def mock_drive_read_doc_html(token, doc_id):
        return "<h1>Chiang Mai Design Week</h1><p>Content</p>"

    async def mock_generate_event_seo(title, body_text, category="Events"):
        return {
            "keyphrases": ["Chiang Mai Design Week"],
            "metas": ["Meta description"],
            "hashtags": "",
            "ai_a": "",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            return {"id": 1234, "link": "https://www.thailandnow.in.th/event/cmdw"}
        return {}

    captured_updates = []

    async def mock_pipeline_find_row(slug):
        return 7

    async def mock_sheet_update_cell(sheet_id, tab, row_number, col_letter, value):
        captured_updates.append((sheet_id, tab, row_number, col_letter, value))

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)
    monkeypatch.setattr("app.thailandnow._pipeline_find_row", mock_pipeline_find_row)
    monkeypatch.setattr("app.thailandnow._sheet_update_cell", mock_sheet_update_cell)

    res = await publish_event_from_card({"card_id": "card_flip"})
    assert res["wp_id"] == 1234
    assert res["registry"] == "draft-logged"
    assert len(captured_updates) == 1
    sheet_id, tab, row_num, col_letter, value = captured_updates[0]
    assert sheet_id == COVERED_OURS_SHEET
    assert tab == "Pipeline"
    assert row_num == 7
    assert col_letter == "F"
    assert value == "DRAFT"


@pytest.mark.anyio
async def test_covered_events_pipeline_precedence(monkeypatch):
    from app.thailandnow import _covered_events, COVERED_OURS_SHEET, COVERED_COMPANY_SHEET

    async def mock_read_sheet_col(sheet_id, range_param, col=0, header_rows=1):
        if sheet_id == COVERED_OURS_SHEET:
            return ["Songkran Festival", "Loy Krathong"]
        if sheet_id == COVERED_COMPANY_SHEET:
            return ["[202608] [EN] Bangkok Art Biennale", "[202608] [EN] Songkran Festival"]
        return []

    async def mock_sheet_read_all(sheet_id, tab):
        if sheet_id == COVERED_OURS_SHEET and tab == "Pipeline":
            return [
                ["Provisioned At", "Event Title", "Slug", "Trello Card", "Doc Link", "Status"],
                ["2026-08-18", "Songkran Festival", "songkranfestival", "card1", "doc1", "DRAFT"],
                ["2026-08-18", "Phuket Veg Fest", "phuketvegfest", "card2", "doc2", "PIPELINE"],
            ]
        return []

    monkeypatch.setattr("app.thailandnow._read_sheet_col", mock_read_sheet_col)
    monkeypatch.setattr("app.thailandnow._sheet_read_all", mock_sheet_read_all)

    covered, errors = await _covered_events()
    assert errors == []
    # Ours wins over pipeline for Songkran Festival
    assert covered["songkranfestival"] == "ours"
    assert covered["loykrathong"] == "ours"
    assert covered["bangkokartbiennale"] == "company"
    # Phuket Veg Fest is pipeline
    assert covered["phuketvegfest"] == "pipeline"


@pytest.mark.anyio
async def test_sync_events_registry(monkeypatch):
    from app.thailandnow import sync_events_registry, COVERED_OURS_SHEET

    mock_wp_events = [
        {"id": 101, "date": "2026-08-01", "slug": "event-one", "link": "https://tn.th/e1", "title": "Event One"},
        {"id": 102, "date": "2026-08-10", "slug": "event-two", "link": "https://tn.th/e2", "title": "Event Two"},
    ]

    async def mock_wp_pull():
        return mock_wp_events

    # Current published tab had 4 rows (header + 3 data) -> max_rows should pad to max(3, 4+1) = 5
    curr_published_rows = [
        ["Header", "Date", "Slug", "Link", "ID"],
        ["Old 1", "2026-01-01", "old-1", "link1", "1"],
        ["Old 2", "2026-01-02", "old-2", "link2", "2"],
        ["Old 3", "2026-01-03", "old-3", "link3", "3"],
    ]

    # Pipeline tab has 3 rows: header, row 2 matches event-one, row 3 doesn't match
    pipeline_rows = [
        ["Provisioned At", "Event Title", "Slug", "Trello Card", "Doc Link", "Status"],
        ["2026-08-18", "Event One", "eventone", "card1", "doc1", "PIPELINE"],
        ["2026-08-18", "Unpublished", "unpublished", "card2", "doc2", "PIPELINE"],
    ]

    captured_updates = []
    captured_cells = []

    async def mock_sheet_read_all(sheet_id, tab):
        if not tab:
            return curr_published_rows
        if tab == "Pipeline":
            return pipeline_rows
        return []

    async def mock_sheet_update_range(sheet_id, tab, range_param, rows):
        captured_updates.append((sheet_id, tab, range_param, rows))

    async def mock_sheet_update_cell(sheet_id, tab, row_number, col_letter, value):
        captured_cells.append((sheet_id, tab, row_number, col_letter, value))

    async def mock_ensure_pipeline_tab():
        pass

    monkeypatch.setattr("app.thailandnow._wp_pull_published_events", mock_wp_pull)
    monkeypatch.setattr("app.thailandnow._sheet_read_all", mock_sheet_read_all)
    monkeypatch.setattr("app.thailandnow._sheet_update_range", mock_sheet_update_range)
    monkeypatch.setattr("app.thailandnow._sheet_update_cell", mock_sheet_update_cell)
    monkeypatch.setattr("app.thailandnow._ensure_pipeline_tab", mock_ensure_pipeline_tab)

    res = await sync_events_registry()

    assert res == {"published_synced": 2, "pipeline_flipped": 1}

    # Verify padded write
    assert len(captured_updates) == 1
    sheet_id, tab, rng, rows = captured_updates[0]
    assert sheet_id == COVERED_OURS_SHEET
    assert tab == ""
    assert rng == "A1:E5"
    assert len(rows) == 5
    assert rows[0] == ["Event Title", "Date Published (WP)", "Slug", "WP Link", "WP ID"]
    assert rows[1] == ["Event One", "2026-08-01", "event-one", "https://tn.th/e1", "101"]
    assert rows[2] == ["Event Two", "2026-08-10", "event-two", "https://tn.th/e2", "102"]
    assert rows[3] == ["", "", "", "", ""]
    assert rows[4] == ["", "", "", "", ""]

    # Verify pipeline flipped
    assert len(captured_cells) == 1
    sheet_id, tab, row_num, col_letter, value = captured_cells[0]
    assert sheet_id == COVERED_OURS_SHEET
    assert tab == "Pipeline"
    assert row_num == 2
    assert col_letter == "F"
    assert value == "PUBLISHED"


@pytest.mark.anyio
async def test_sync_events_registry_empty_pull_never_wipes(monkeypatch):
    """WP outage on page 1 → no events → the Published tab rewrite must be skipped
    entirely (a header-only rewrite would blank the registry)."""
    from app.thailandnow import sync_events_registry

    async def mock_wp_pull():
        return []

    called = []

    async def mock_sheet_update_range(sheet_id, tab, range_param, rows):
        called.append((tab, range_param))

    async def mock_ensure_pipeline_tab():
        called.append(("ensure",))

    monkeypatch.setattr("app.thailandnow._wp_pull_published_events", mock_wp_pull)
    monkeypatch.setattr("app.thailandnow._sheet_update_range", mock_sheet_update_range)
    monkeypatch.setattr("app.thailandnow._ensure_pipeline_tab", mock_ensure_pipeline_tab)

    res = await sync_events_registry()

    assert res["published_synced"] == 0
    assert res["pipeline_flipped"] == 0
    assert "skipped" in res
    assert called == []  # no sheet write of any kind happened


@pytest.mark.anyio
async def test_publish_event_from_card_hook_a_appends_new_row(monkeypatch):
    from app.thailandnow import publish_event_from_card, COVERED_OURS_SHEET

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_new":
            return {"id": "card_new", "name": "Event | Krabi Rock Festival", "desc": "Card desc"}
        if path == "/cards/card_new/attachments":
            return [{"name": "Doc", "url": "https://docs.google.com/document/d/DOC_KRF/edit"}]
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        return "Krabi Rock Festival plain text"

    async def mock_drive_read_doc_html(token, doc_id):
        return "<h1>Krabi Rock Festival</h1><p>Content</p>"

    async def mock_generate_event_seo(title, body_text, category="Events"):
        return {
            "keyphrases": ["Krabi Rock Festival"],
            "metas": ["Meta description"],
            "hashtags": "",
            "ai_a": "",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            return {"id": 5678, "link": "https://www.thailandnow.in.th/event/krabi-rock"}
        return {}

    captured_appends = []

    async def mock_pipeline_find_row(slug):
        return None

    async def mock_sheet_append_rows(sheet_id, tab, rows):
        captured_appends.append((sheet_id, tab, rows))

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)
    monkeypatch.setattr("app.thailandnow._pipeline_find_row", mock_pipeline_find_row)
    monkeypatch.setattr("app.thailandnow._sheet_append_rows", mock_sheet_append_rows)

    res = await publish_event_from_card({"card_id": "card_new"})
    assert res["wp_id"] == 5678
    assert res["registry"] == "draft-logged"
    assert len(captured_appends) == 1
    sheet_id, tab, rows = captured_appends[0]
    assert sheet_id == COVERED_OURS_SHEET
    assert tab == "Pipeline"
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == f"Krabi Rock Festival {datetime.now().year}"
    assert row[2] == f"krabirockfestival{datetime.now().year}"
    assert row[3] == ""
    assert row[4] == "https://www.thailandnow.in.th/event/krabi-rock"
    assert row[5] == "DRAFT"


@pytest.mark.anyio
async def test_publish_article_from_card_does_not_log_to_pipeline(monkeypatch):
    from app.thailandnow import publish_event_from_card

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_art":
            return {"id": "card_art", "name": "Article | Thai Silk Story", "desc": "Article desc"}
        if path == "/cards/card_art/attachments":
            return [{"name": "Doc", "url": "https://docs.google.com/document/d/DOC_SILK/edit"}]
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        return "Thai Silk Story text"

    async def mock_drive_read_doc_html(token, doc_id):
        return "<h1>Thai Silk Story</h1><p>Silk content</p>"

    async def mock_generate_event_seo(title, body_text, category="Articles"):
        return {
            "keyphrases": ["Thai Silk"],
            "metas": ["Meta description"],
            "hashtags": "",
            "ai_a": "",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/posts":
            return {"id": 9012, "link": "https://www.thailandnow.in.th/thai-silk"}
        return {}

    captured_updates = []
    captured_appends = []

    async def mock_sheet_update_cell(sheet_id, tab, row_number, col_letter, value):
        captured_updates.append((sheet_id, tab, row_number, col_letter, value))

    async def mock_sheet_append_rows(sheet_id, tab, rows):
        captured_appends.append((sheet_id, tab, rows))

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)
    monkeypatch.setattr("app.thailandnow._sheet_update_cell", mock_sheet_update_cell)
    monkeypatch.setattr("app.thailandnow._sheet_append_rows", mock_sheet_append_rows)

    res = await publish_event_from_card({"card_id": "card_art"})
    assert res["wp_id"] == 9012
    assert res["kind"] == "article"
    assert "registry" not in res
    assert len(captured_updates) == 0
    assert len(captured_appends) == 0


@pytest.mark.anyio
async def test_create_event_doc_soft_fail_on_sheet_error(monkeypatch):
    from app.thailandnow import create_event_doc

    async def mock_provision(payload):
        return {
            "desk_id": "tian",
            "count": 1,
            "yyyymm": "202608",
            "items": [
                {
                    "nn": "01",
                    "doc_name": '[202608] [EN] "Fail Fest"',
                    "doc_url": "https://docs.google.com/document/d/DOC_FAIL",
                    "card_name": "Event | Fail Fest",
                    "card_url": "https://trello.com/c/CARD_FAIL",
                }
            ],
        }

    async def mock_ensure_pipeline_fail():
        raise RuntimeError("Google Sheets quota exceeded")

    monkeypatch.setattr("app.thailandnow.provision", mock_provision)
    monkeypatch.setattr("app.thailandnow._ensure_pipeline_tab", mock_ensure_pipeline_fail)

    payload = {
        "event": {
            "title": "Fail Fest",
            "url": "https://failfest.bkk/2026",
            "start_date": "2026-08-20",
        },
        "bundle_text": "Bundle content",
    }
    res = await create_event_doc(payload)
    assert res["desk_id"] == "tian"
    assert res["registry"] == "skipped: Google Sheets quota exceeded"


@pytest.mark.anyio
async def test_publish_event_from_card_soft_fail_on_sheet_error(monkeypatch):
    from app.thailandnow import publish_event_from_card

    async def mock_trello(method, path, params=None, body=None):
        if path == "/cards/card_err":
            return {"id": "card_err", "name": "Event | Error Fest", "desc": "Card desc"}
        if path == "/cards/card_err/attachments":
            return [{"name": "Doc", "url": "https://docs.google.com/document/d/DOC_ERR/edit"}]
        return {}

    async def mock_google_token():
        return "mock_token"

    async def mock_drive_read_doc(token, doc_id):
        return "Error Fest plain text"

    async def mock_drive_read_doc_html(token, doc_id):
        return "<h1>Error Fest</h1><p>Content</p>"

    async def mock_generate_event_seo(title, body_text, category="Events"):
        return {
            "keyphrases": ["Error Fest"],
            "metas": ["Meta description"],
            "hashtags": "",
            "ai_a": "",
            "ai_b": ["Takeaway 1"],
        }, "gemini-3.6-flash (agy)"

    async def mock_wp(method, path, params=None, json_body=None):
        if method == "POST" and path == "/event":
            return {"id": 7777, "link": "https://www.thailandnow.in.th/event/err"}
        return {}

    async def mock_pipeline_find_row_err(slug):
        raise RuntimeError("Sheets 503 backend error")

    monkeypatch.setattr("app.thailandnow._trello", mock_trello)
    monkeypatch.setattr("app.thailandnow._google_token", mock_google_token)
    monkeypatch.setattr("app.thailandnow._drive_read_doc", mock_drive_read_doc)
    monkeypatch.setattr("app.thailandnow._drive_read_doc_html", mock_drive_read_doc_html)
    monkeypatch.setattr("app.thailandnow._generate_event_seo", mock_generate_event_seo)
    monkeypatch.setattr("app.thailandnow._wp", mock_wp)
    monkeypatch.setattr("app.thailandnow._pipeline_find_row", mock_pipeline_find_row_err)

    res = await publish_event_from_card({"card_id": "card_err"})
    assert res["wp_id"] == 7777
    assert res["registry"] == "skipped: Sheets 503 backend error"

