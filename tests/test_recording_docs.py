"""RECORDING DOCS — extraction + title transform tests (no network)."""

from app import recording_docs as rd


def _para(text, style="NORMAL_TEXT", bold=False):
    runs = [{"textRun": {"content": text,
                         "textStyle": {"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}} if bold else {}}}]
    return {"paragraph": {"elements": runs, "paragraphStyle": {"namedStyleType": style}}}


def _table(cell_paras):
    return {"table": {"tableRows": [{"tableCells": [{"content": cell_paras}]}]}}


def _line(block):
    return "".join(t for t, _ in block["runs"])


def test_wrap_title_strip_tag_uppercase_midpoint():
    out = rd._wrap_title("5", [("PRD Upgrades Crisis Information Center to JICC", {})])
    assert [_line(b) for b in out] == ["5. PRD UPGRADES CRISIS INFORMATION CENTER TO JICC"]
    # 8-word title wraps with WRAP_AT_WORDS = 7
    eight = rd._wrap_title("3", [("Prime Minister Visits Flood Hit Areas in Chiang Rai", {})])
    assert len(eight) == 2
    two = rd._wrap_title("2", [("[ต้นน้ำ] Cabinet Approval Sought to Build 504 km of Southern Double Track Railway", {})])
    assert len(two) == 2
    assert _line(two[0]).startswith("2. CABINET APPROVAL")
    assert "ต้นน้ำ" not in _line(two[0]) + _line(two[1])
    assert _line(two[1]) == _line(two[1]).upper()
    # break avoids landing right after a preposition
    br = rd._wrap_title("1", [("Thai Soldier Injured in Landmine Blast in Ubon Ratchathani", {})])
    assert not _line(br[0]).rstrip().endswith((" IN", " OF", " THE", " TO", " AND"))


def test_extract_rundown_eve_shape_styled():
    els = [
        _para("NBT WORLD BRIEF EVENING / 17.AUGUST.2026", style="TITLE", bold=True),
        _table([_para("***JINGLE NWTB*** ชื่อข่าว/CG", bold=True)]),
        _para("ผู้ประกาศ:"),
        _para(" / "),
        _para("1. Thai Soldier Injured in Landmine Blast in Ubon Ratchathani"),
        _para("ทหารไทยเหยียบทุ่นระเบิดช่องอานม้า"),  # Thai summary → skipped
        _para("2. Cabinet Approval Sought to Build 504 km of Southern Double Track Railway"),
        _para("SB1: Prof. Dr. Wilert Puriwat"),
        _para("        President, Chulalongkorn University"),
        _table([_para("***โยนเบรค/โยนกลับไปผปก.ไทย***")]),  # ends the list, dropped
    ]
    out = rd._extract_rundown(els)
    assert out["header"] == "NBT WORLD BRIEF EVENING / 17.AUGUST.2026"
    assert out["header_style"].get("namedStyleType") == "TITLE"
    kinds = [b["kind"] for b in out["blocks"]]
    assert kinds[0] == "table"
    texts = [_line(b) for b in out["blocks"] if b["kind"] == "para"]
    assert "ผู้ประกาศ:" in texts and " / " in texts
    assert texts[2].startswith("1. THAI SOLDIER INJURED")
    assert any(t.startswith("2. CABINET APPROVAL") for t in texts)
    assert "SB1: PROF. DR. WILERT PURIWAT" in texts
    assert any(t.strip() == "PRESIDENT, CHULALONGKORN UNIVERSITY" for t in texts)
    assert not any("ทหารไทย" in t for t in texts)
    assert not any("โยนเบรค" in _cell for b in out["blocks"] if b["kind"] == "table"
                   for _cell in [rd._cell_text(b["cell"])])
    # styles travel with the copy
    tb = out["blocks"][0]
    assert tb["cell"][0]["runs"][0][1].get("bold") is True


def test_space_rundown_blocks_apply_spacing():
    blocks = [
        {"kind": "table", "cell": [[{"runs": [("***JINGLE***", {})]}]]},
        {"kind": "para", "runs": [("ผู้ประกาศ:", {})], "para_style": None},
        {"kind": "para", "runs": [(" / ", {})], "para_style": None},
        {"kind": "para", "runs": [("1. FIRST STORY", {})], "para_style": None},
        {"kind": "para", "runs": [("2. SECOND STORY LINE 1", {})], "para_style": None},
        {"kind": "para", "runs": [("SECOND STORY LINE 2", {})], "para_style": None},
        {"kind": "para", "runs": [("SB1: PROFESSOR NAME", {})], "para_style": None},
        {"kind": "para", "runs": [("        PRESIDENT OF UNIVERSITY", {})], "para_style": None},
        {"kind": "table", "cell": [[{"runs": [("***END CREDIT***", {})]}]]},
    ]
    spaced = rd._space_rundown_blocks(blocks)
    texts = [("".join(t for t, _ in b["runs"]) if b["kind"] == "para" else "[TABLE]") for b in spaced]
    assert texts == [
        "[TABLE]",
        "ผู้ประกาศ:",
        " / ",
        "1. FIRST STORY",
        " ",
        "2. SECOND STORY LINE 1",
        "SECOND STORY LINE 2",
        "SB1: PROFESSOR NAME",
        "        PRESIDENT OF UNIVERSITY",
        "[TABLE]",
    ]


def test_extract_prompter_shape_with_intermission_and_no_dedup():
    els = [
        _para("NEWSLINE 17.AUGUST.2026 -- ANCHOR: ", style="TITLE"),
        _table([_para("***JINGLE NEWSLINE ตัวยาว***")]),
        _table([_para("***JINGLE NEWSLINE ตัวยาว***", bold=True),
                _para("SWDK. Thank you for joining us for Newsline. In our first story tonight, … PM has departed.////")]),
        _para("1. PM departs for Sydney", style="HEADING_1"),
        _table([_para("Prime Minister Anutin has departed Bangkok for Sydney.")]),
        _para("2. Soldier injured", style="HEADING_1"),
        _table([_para("Another Thai soldier has lost part of his right leg.", bold=True)]),
        _table([_para("*** 1st BREAK ***")]),  # bare table between story 2 and story 3 -> intermission
        _para("3. Corgis", style="HEADING_1"),
        _table([_para("Around 150 corgis gathered in Vilnius.")]),
        _para("ผู้ประกาศพูดลา แบบจบเบรค", style="HEADING_1"),
        _table([_para("That brings us to the end of this evening's NBT World Brief.")]),
        _table([_para("Thank you for joining us tonight. ***END CREDIT***")]),  # bare ending table
    ]
    p = rd._extract_prompter(els)
    assert "rows" in p
    assert len(p["rows"]) == 4
    # All rows in order: story 1 intro, story 2 intro, intermission, story 3 intro
    assert p["rows"][0]["type"] == "intro"
    assert "Prime Minister Anutin" in "".join(t for para in p["rows"][0]["cell"] for t, _ in para["runs"])
    assert p["rows"][1]["type"] == "intro"
    assert p["rows"][2]["type"] == "intermission"
    assert "*** 1st BREAK ***" in "".join(t for para in p["rows"][2]["cell"] for t, _ in para["runs"])
    assert p["rows"][3]["type"] == "intro"
    assert "Around 150 corgis" in "".join(t for para in p["rows"][3]["cell"] for t, _ in para["runs"])

    open_text = "".join(t for para in p["open"] for t, _ in para["runs"])
    assert "Thank you for joining us for Newsline" in open_text
    assert "".join(t for t, _ in p["outro"][0]["runs"]).startswith("That brings us")
    assert "".join(t for t, _ in p["ending"][0]["runs"]).startswith("Thank you for joining us tonight")
    # styled runs preserved
    assert any(s.get("bold") for para in p["rows"][1]["cell"] for _, s in [(0, para["runs"][0][1])])


def test_anchor_name_extracted_from_drive_export(monkeypatch):
    def mock_api(method, url, body=None, params=None, headers=None, raw_response=False):
        assert "export" in url
        assert params == {"mimeType": "text/plain"}
        assert raw_response is True
        return b"\xef\xbb\xbfNEWSLINE 14.AUGUST.2026 -- ANCHOR: SANDRA H.\n"

    monkeypatch.setattr(rd, "_api", mock_api)
    name = rd._anchor_name("1_some_script_doc_id")
    assert name == "SANDRA H."


def test_anchor_name_empty_on_missing_or_error(monkeypatch):
    def mock_api_err(*args, **kwargs):
        raise RuntimeError("Drive export failed")

    monkeypatch.setattr(rd, "_api", mock_api_err)
    assert rd._anchor_name("1_bad_id") == ""


def test_row_heads():
    cells = [
        [{"runs": [("Hello ", {}), ("World", {"bold": True})]}],
        [{"runs": []}],
    ]
    assert rd._row_heads(cells) == ["Hello World", "(empty)"]
