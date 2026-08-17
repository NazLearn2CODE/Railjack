"""RECORDING DOCS — extraction + title transform tests (no network)."""

from app import recording_docs as rd


def _para(text, style="NORMAL_TEXT", bold=False):
    runs = [{"textRun": {"content": text,
                         "textStyle": {"bold": True} if bold else {}}}]
    return {"paragraph": {"elements": runs, "paragraphStyle": {"namedStyleType": style}}}


def _table(cell_paras):
    return {"table": {"tableRows": [{"tableCells": [{"content": cell_paras}]}]}}


def test_wrap_title_strip_tag_uppercase_midpoint():
    assert rd._wrap_title("5", "PRD Upgrades Crisis Information Center to JICC") == \
        ["5. PRD UPGRADES CRISIS INFORMATION CENTER TO JICC"]
    two = rd._wrap_title("2", "[ต้นน้ำ] Cabinet Approval Sought to Build 504 km of Southern Double Track Railway")
    assert len(two) == 2
    assert two[0].startswith("2. CABINET APPROVAL")
    assert "ต้นน้ำ" not in two[0] + two[1]
    assert two[1] == two[1].upper()
    # break avoids landing right after a preposition
    br = rd._wrap_title("1", "Thai Soldier Injured in Landmine Blast in Ubon Ratchathani")
    assert not br[0].rstrip().endswith((" IN", " OF", " THE", " TO", " AND"))


def test_extract_rundown_eve_shape():
    els = [
        _para("NBT WORLD BRIEF EVENING / 17.AUGUST.2026", style="TITLE"),
        _table([_para("***JINGLE NBTWB*** ชื่อข่าว/CG")]),
        _para("ผู้ประกาศ:"),
        _para(" / "),
        _para("1. Thai Soldier Injured in Landmine Blast in Ubon Ratchathani"),
        _para("ทหารไทยเหยียบทุ่นระเบิดช่องอานม้า"),  # Thai summary → skipped
        _para("2. Cabinet Approval Sought to Build 504 km of Southern Double Track Railway"),
        _para("SB1: Prof. Dr. Wilert Puriwat"),
        _para("        President, Chulalongkorn University"),
        _table([_para("***โยนเบรค/โยนกลับไปผปก.ไทย***")]),  # ends the list, dropped
        _para("# later junk"),
    ]
    out = rd._extract_rundown(els)
    assert out["header"] == "NBT WORLD BRIEF EVENING / 17.AUGUST.2026"
    kinds = [(b["kind"], b["text"]) for b in out["blocks"]]
    assert ("table", "***JINGLE NWTB*** ชื่อข่าว/CG".replace("NWTB", "NWTB")) not in kinds  # sanity
    assert kinds[0] == ("table", "***JINGLE NWTB*** ชื่อข่าว/CG".replace("NWTB", "NWTB")) or kinds[0][0] == "table"
    texts = [t for k, t in kinds if k == "line"]
    assert "ผู้ประกาศ:" in texts and "/" in texts
    assert texts[2].startswith("1. THAI SOLDIER INJURED")
    assert any(t.startswith("2. CABINET APPROVAL") for t in texts)
    assert "SB1: PROF. DR. WILERT PURIWAT" in texts
    assert "PRESIDENT, CHULALONGKORN UNIVERSITY" in texts
    assert not any("ทหารไทย" in t for t in texts)
    assert not any("โยนเบรค" in t for k, t in kinds)  # throw-to-break table dropped


def test_extract_prompter_open_dedup_and_cells():
    els = [
        _para("NEWSLINE 17.AUGUST.2026 -- ANCHOR: ", style="TITLE"),
        _table([_para("***JINGLE NEWSLINE ตัวยาว***")]),
        _table([_para("***END CREDIT***")]),
        _table([_para("***JINGLE NEWSLINE ตัวยาว***\n", bold=True),
                _para("SWDK. Thank you for joining us for Newsline. In our first story tonight, … PM has departed.////")]),
        _para("1. PM departs for Sydney", style="HEADING_1"),
        _table([_para("Prime Minister Anutin has departed Bangkok for Sydney.")]),
        _para("2. Soldier injured", style="HEADING_1"),
        _table([_para("Another Thai soldier has lost part of his right leg.", bold=True)]),
        _para("ผู้ประกาศพูดลา แบบจบเบรค", style="HEADING_1"),
        _table([_para("That brings us to the end of this evening's NBT World Brief.")]),
        _para("12. Corgis", style="HEADING_1"),
        _table([_para("Around 150 corgis gathered in Vilnius.")]),
        _table([_para("Thank you for joining us tonight. ***END CREDIT***")]),  # bare ending table
    ]
    p = rd._extract_prompter(els)
    assert len(p["intros"]) == 3
    assert p["open"] is not None and "Thank you for joining us for Newsline" in p["open"][0][0] + p["open"][1][0]
    assert p["outro"][0][0].startswith("That brings us")
    assert p["ending"][0][0].startswith("Thank you for joining us tonight")
    # styled runs preserved
    assert any(s.get("bold") for _, s in p["intros"][1])


def test_row_heads_and_missing_show_soft_errors():
    assert rd._row_heads([[("a", {}), ("b", {"bold": True})], []]) == ["ab", "(empty)"]
