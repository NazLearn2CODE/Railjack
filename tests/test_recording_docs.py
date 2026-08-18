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


def _snapshot(elements: list[dict], start_idx: int = 1) -> list[dict]:
    """Build synthetic elements with valid, contiguous startIndex and endIndex."""
    import copy
    out = []
    idx = start_idx
    for e in elements:
        c = copy.deepcopy(e)
        c["startIndex"] = idx
        if "paragraph" in c:
            text = "".join(el.get("textRun", {}).get("content", "") for el in c["paragraph"]["elements"])
            if not text.endswith("\n"):
                text += "\n"
                c["paragraph"]["elements"][-1]["textRun"]["content"] = text
            idx += len(text)
        elif "table" in c:
            idx += 40
        c["endIndex"] = idx
        out.append(c)
    return out


def _apply_simulated_reqs(reqs: list[dict], original_elements: list[dict]) -> str:
    """Apply batchUpdate requests against a simulated string buffer to check delta consistency."""
    total_len = original_elements[-1]["endIndex"] if original_elements else 0
    buf = [" "] * (total_len + 10)
    for e in original_elements:
        if "paragraph" in e:
            text = "".join(el.get("textRun", {}).get("content", "") for el in e["paragraph"]["elements"])
            for offset, ch in enumerate(text):
                buf[e["startIndex"] + offset] = ch
        elif "table" in e:
            t_text = rd._table_text(e) or "[TABLE]"
            padded = t_text.ljust(e["endIndex"] - e["startIndex"])[:e["endIndex"] - e["startIndex"]]
            for offset, ch in enumerate(padded):
                buf[e["startIndex"] + offset] = ch

    s = "".join(buf)
    for r in reqs:
        if "deleteContentRange" in r:
            rg = r["deleteContentRange"]["range"]
            start, end = rg["startIndex"], rg["endIndex"]
            assert start >= 0 and end >= start, f"Invalid delete range: {start}..{end}"
            s = s[:start] + s[end:]
        elif "insertText" in r:
            loc = r["insertText"]["location"]
            idx = loc["index"]
            text = r["insertText"]["text"]
            assert idx >= 0, f"Invalid insert index: {idx}"
            s = s[:idx] + text + s[idx:]

    return s


def test_rundown_edit_requests_nl():
    raw_els = [
        _para("NEWSLINE 17.AUGUST.2026 -- ANCHOR: ", style="TITLE"),
        _table([_para("***JINGLE NEWSLINE***")]),
        _para("1. [ต้นน้ำ] Cabinet Approval Sought to Build 504 km of Southern Double Track Railway"),
        _para("ทหารไทยเหยียบทุ่นระเบิดช่องอานม้า"),
        _para("SB1: Prof. Dr. Wilert Puriwat"),
        _para("        President, Chulalongkorn University"),
        _table([_para("***END CREDIT***")]),
        _para("1. Cabinet Approval", style="HEADING_1"),
        _table([_para("Full story body text...")]),
        _para(""),
    ]
    els = _snapshot(raw_els)
    reqs = rd._rundown_edit_requests(els, anchor_name="SANDRA H.", tab_id="t.0", is_nl=True)

    # Anchor insertText
    anchor_req = next((r for r in reqs if "insertText" in r and "SANDRA H." in r["insertText"]["text"]), None)
    assert anchor_req is not None
    assert anchor_req["insertText"]["location"]["tabId"] == "t.0"

    # Title replacement
    title_ins = next((r for r in reqs if "insertText" in r and "CABINET APPROVAL" in r["insertText"]["text"]), None)
    assert title_ins is not None
    assert "ต้นน้ำ" not in title_ins["insertText"]["text"]
    assert "\n" in title_ins["insertText"]["text"]

    # Bulk delete — descending emission puts the highest range first
    bulk_del = reqs[0]
    assert "deleteContentRange" in bulk_del
    assert bulk_del["deleteContentRange"]["range"]["tabId"] == "t.0"

    # Delta consistency
    simulated_text = _apply_simulated_reqs(reqs, els)
    assert "NEWSLINE 17.AUGUST.2026 -- ANCHOR: SANDRA H." in simulated_text
    assert "1. CABINET APPROVAL SOUGHT TO BUILD 504" in simulated_text
    assert "KM OF SOUTHERN DOUBLE TRACK RAILWAY" in simulated_text
    assert "SB1: PROF. DR. WILERT PURIWAT" in simulated_text
    assert "PRESIDENT, CHULALONGKORN UNIVERSITY" in simulated_text
    assert "ทหารไทย" not in simulated_text
    assert "Full story body text" not in simulated_text


def test_rundown_edit_requests_eve():
    raw_els = [
        _para("NBT WORLD BRIEF EVENING / 17.AUGUST.2026", style="TITLE"),
        _table([_para("***JINGLE NWTB***")]),
        _para("ผู้ประกาศ:"),
        _para(" / "),
        _para("1. Thai Soldier Injured in Landmine Blast in Ubon Ratchathani"),
        _para("ทหารไทยเหยียบทุ่นระเบิด"),
        _table([_para("***โยนเบรค/โยนกลับไปผปก.ไทย***")]),
        _para("Story 1 Full Script", style="HEADING_1"),
        _para(""),
    ]
    els = _snapshot(raw_els)
    reqs = rd._rundown_edit_requests(els, anchor_name="", tab_id="t.1", is_nl=False)

    simulated_text = _apply_simulated_reqs(reqs, els)
    assert "ผู้ประกาศ:" in simulated_text
    assert " / " in simulated_text
    assert "1. THAI SOLDIER INJURED" in simulated_text
    assert "ทหารไทย" not in simulated_text
    assert "Story 1 Full Script" not in simulated_text


def test_prompter_edit_requests_nl():
    raw_els = [
        _para("NEWSLINE 17.AUGUST.2026 -- ANCHOR: ", style="TITLE"),
        _table([_para("***JINGLE NEWSLINE ตัวยาว***")]),
        _table([_para("Show Open Table Content")]),
        _para("1. PM departs for Sydney", style="HEADING_1"),
        _table([_para("Story 1 Intro Table Content")]),
        _para("2. Soldier injured", style="HEADING_1"),
        _table([_para("Story 2 Intro Table Content")]),
        _table([_para("*** 1st BREAK ***")]),
        _para("3. Corgis", style="HEADING_1"),
        _table([_para("Story 3 Intro Table Content")]),
        _para("ผู้ประกาศพูดลา แบบจบเบรค", style="HEADING_1"),
        _table([_para("Outro Table Content")]),
        _table([_para("Thank you for joining us tonight. ***END CREDIT***")]),
        _para(""),
    ]
    els = _snapshot(raw_els)
    roles = rd._classify_prompter_tables(els, is_eve=False)
    role_map = [r for _, r in roles]
    assert role_map == ["drop", "open", "intro", "intro", "intermission", "intro", "outro", "ending"]

    reqs = rd._prompter_edit_requests(els, roles, tab_id="t.0")
    simulated_text = _apply_simulated_reqs(reqs, els)

    assert "Show Open Table Content" in simulated_text
    assert "Story 1 Intro Table Content" in simulated_text
    assert "Story 2 Intro Table Content" in simulated_text
    assert "*** 1st BREAK ***" in simulated_text
    assert "Story 3 Intro Table Content" in simulated_text
    assert "Outro Table Content" in simulated_text
    assert "Thank you for joining us tonight" in simulated_text

    assert "NEWSLINE 17.AUGUST.2026" not in simulated_text
    assert "***JINGLE NEWSLINE ตัวยาว***" not in simulated_text
    assert "PM departs for Sydney" not in simulated_text
    assert "Soldier injured" not in simulated_text
    assert "Corgis" not in simulated_text
    assert "ผู้ประกาศพูดลา" not in simulated_text


def test_prompter_edit_requests_eve():
    raw_els = [
        _para("NBT WORLD BRIEF EVENING / 17.AUGUST.2026", style="TITLE"),
        _table([_para("***JINGLE NWTB***")]),
        _para("1. Soldier Injured", style="HEADING_1"),
        _table([_para("EVE Story 1 Intro Content")]),
        _para("ผู้ประกาศพูดลา", style="HEADING_1"),
        _table([_para("EVE Outro Content")]),
        _para(""),
    ]
    els = _snapshot(raw_els)
    roles = rd._classify_prompter_tables(els, is_eve=True)
    role_map = [r for _, r in roles]
    assert role_map == ["drop", "intro", "outro"]

    reqs = rd._prompter_edit_requests(els, roles, tab_id="t.1")
    simulated_text = _apply_simulated_reqs(reqs, els)

    assert "***JINGLE NWTB***" not in simulated_text
    assert "EVE Story 1 Intro Content" in simulated_text
    assert "EVE Outro Content" in simulated_text


def test_preview_recording_docs_target_names(monkeypatch):
    def mock_extract(script_doc_id):
        return {
            "script_doc": "doc123",
            "date": "2026-08-17",
            "rundown_name": "RUNDOWN NL-NWB 170826",
            "prompter_name": "PROMPTER NL-NWB 170826",
            "anchor_name": "SANDRA H.",
            "eve_announcer": "ผปก. EVE",
            "eve": {"header": "EVE HEADER", "blocks": []},
            "nl": {"header": "NL HEADER", "blocks": []},
            "prompter_eve": {"rows": [], "outro": None},
            "prompter_nl": {"rows": [], "open": None, "ending": None},
            "errors": [],
            "warnings": [],
        }

    monkeypatch.setattr(rd, "extract_recording_docs", mock_extract)
    res = rd.preview_recording_docs("doc123")
    assert res["target_names"] == ["RUNDOWN NL-NWB 170826", "PROMPTER NL-NWB 170826"]
    assert "rundown_doc" not in res
    assert "prompter_doc" not in res


def test_rundown_edit_requests_with_anchors_section_preserved():
    raw_els = [
        _para("NEWSLINE 17.AUGUST.2026 -- ANCHOR: ", style="TITLE"),
        _para("1. First Story"),
        _table([_para("***END CREDIT***")]),
        _para("Story Body Text (to be deleted)", style="HEADING_1"),
        _para("⚓ ANCHORS", style="HEADING_1"),
        _para("Anchor Bio and Line"),
        _para(""),
    ]
    els = _snapshot(raw_els)
    reqs = rd._rundown_edit_requests(els, anchor_name="", tab_id="t.0", is_nl=True)

    simulated_text = _apply_simulated_reqs(reqs, els)
    assert "1. FIRST STORY" in simulated_text
    assert "***END CREDIT***" in simulated_text
    assert "Story Body Text" not in simulated_text
    assert "⚓ ANCHORS" in simulated_text
    assert "Anchor Bio and Line" in simulated_text


def test_ensure_copy_idempotency(monkeypatch):
    calls = []

    def mock_api(method, url, body=None, params=None, headers=None, raw_response=False):
        calls.append((method, url, body, params))
        if method == "GET" and "drive/v3/files" in url:
            return {"files": [{"id": "old_file_id", "name": "RUNDOWN NL-NWB 170826"}]}
        if method == "PATCH" and "drive/v3/files/old_file_id" in url:
            return {"id": "old_file_id", "trashed": True}
        if method == "POST" and "copy" in url:
            return {"id": "new_copy_id", "name": "RUNDOWN NL-NWB 170826"}
        raise RuntimeError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(rd, "_api", mock_api)
    doc_id, url = rd._ensure_copy("source_doc_123", "RUNDOWN NL-NWB 170826", "folder_456")

    assert doc_id == "new_copy_id"
    assert url == "https://docs.google.com/document/d/new_copy_id/edit"
    # Verify old file was trashed
    assert any(c[0] == "PATCH" and "old_file_id" in c[1] and c[2] == {"trashed": True} for c in calls)
    # Verify copy was called with addParents
    copy_call = next(c for c in calls if c[0] == "POST" and "copy" in c[1])
    assert copy_call[3].get("addParents") == "folder_456"
    assert copy_call[2] == {"name": "RUNDOWN NL-NWB 170826"}


def test_apply_recording_docs_mocked(monkeypatch):
    def mock_extract(script_doc_id):
        return {
            "script_doc": "script_123",
            "date": "2026-08-17",
            "rundown_name": "RUNDOWN NL-NWB 170826",
            "prompter_name": "PROMPTER NL-NWB 170826",
            "anchor_name": "SANDRA H.",
            "eve_announcer": "ผปก. EVE",
            "eve": {"header": "EVE HEADER", "blocks": []},
            "nl": {"header": "NL HEADER", "blocks": []},
            "prompter_eve": {"rows": [{"type": "intro"}], "outro": "outro_cell"},
            "prompter_nl": {"rows": [{"type": "intro"}], "open": "open_cell", "ending": "ending_cell"},
            "errors": [],
            "warnings": [],
        }

    monkeypatch.setattr(rd, "extract_recording_docs", mock_extract)

    copies_created = []

    def mock_ensure_copy(src_doc_id, dest_name, folder_id):
        new_id = f"id_for_{dest_name.split()[0].lower()}"
        copies_created.append((dest_name, new_id))
        return new_id, f"https://docs.google.com/document/d/{new_id}/edit"

    monkeypatch.setattr(rd, "_ensure_copy", mock_ensure_copy)

    mock_batches = []

    def mock_batch(doc_id, requests):
        mock_batches.append((doc_id, requests))
        return {}

    monkeypatch.setattr(rd, "_batch", mock_batch)

    def mock_api(method, url, body=None, params=None, headers=None, raw_response=False):
        if method == "GET" and "documents/" in url:
            # Return synthetic snapshot with EVE and NL tabs
            eve_els = _snapshot([
                _para("NBT WORLD BRIEF EVENING / 17.AUGUST.2026", style="TITLE"),
                _table([_para("***JINGLE NWTB***")]),
                _para("1. Story 1"),
                _table([_para("***โยนเบรค***")]),
                _para("Script"),
                _para(""),
            ])
            nl_els = _snapshot([
                _para("NEWSLINE 17.AUGUST.2026 -- ANCHOR: ", style="TITLE"),
                _table([_para("***JINGLE NEWSLINE***")]),
                _para("1. Story 1"),
                _table([_para("***END CREDIT***")]),
                _para("Script"),
                _para(""),
            ])
            return {
                "tabs": [
                    {"tabProperties": {"tabId": "t.eve", "title": "NBTWB EVE RUNDOWN"}, "documentTab": {"body": {"content": eve_els}}},
                    {"tabProperties": {"tabId": "t.nl", "title": "NL RUNDOWN"}, "documentTab": {"body": {"content": nl_els}}},
                ]
            }
        raise RuntimeError(f"Unexpected api call: {method} {url}")

    monkeypatch.setattr(rd, "_api", mock_api)

    res = rd.apply_recording_docs("script_123")
    assert res["applied"] is True
    assert res["rundown_doc"] == "id_for_rundown"
    assert res["prompter_doc"] == "id_for_prompter"
    assert res["rundown_url"] == "https://docs.google.com/document/d/id_for_rundown/edit"
    assert res["prompter_url"] == "https://docs.google.com/document/d/id_for_prompter/edit"
    assert res["rundown_name"] == "RUNDOWN NL-NWB 170826"
    assert res["prompter_name"] == "PROMPTER NL-NWB 170826"
    assert len(mock_batches) == 2
