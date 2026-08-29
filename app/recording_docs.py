"""RECORDING DOCS — build the RUNDOWN + PROMPTER recording docs from a daily script.

From the script doc's "NBTWB EVE RUNDOWN" + "NL RUNDOWN" tabs:
- RUNDOWN doc: both rundown lists copied LINE BY LINE WITH FORMATTING (fonts,
  bold/italic, paragraph styles travel with each run), then reformatted to
  the requirement: titles stripped of [tags], UPPERCASED, long titles wrapped
  ~7 words/line; jingle marker tables and SB/CG lines kept; Thai summary
  lines dropped. The Anchor block at the bottom is preserved untouched.
  The doc is renamed to "RUNDOWN NL-NWB DDMMYY" (date parsed from the script).
- PROMPTER doc: every story's intro cell (the first table under each numbered
  story H1) copied verbatim with formatting, plus the EVE outro, the NL
  show-open table (jingle + open + first story intro in one cell) and the NL
  ending — stacked as 1-row tables, EVE block + "++++" + NL block.

Writes only on apply; preview is read-only. Idempotent (replace-based).
Blocks append at a FIXED end index in forward order — no reverse inserts.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from .newsline_reports import _api, extract_doc_id
except ImportError:  # executed as a script (routes shell out like NEWSLINE_REPORTS)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.newsline_reports import _api, extract_doc_id

# Destination folder for recording docs (default standing docs parent folder)
DEST_FOLDER = "0ABo_r15naExrUk9PVA"

_THAI_RE = re.compile(r"[฀-๿]")
_TAG_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
_NUM_RE = re.compile(r"^\s*(\d+)\.\s*(.*)$")
_DATE_RE = re.compile(r"(\d{1,2})\.([A-Z]+)\.(\d{4})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}
_BREAK_STOPWORDS = {"IN", "OF", "THE", "A", "AN", "AND", "TO", "FOR", "ON",
                    "AT", "WITH", "FROM", "AS", "BY", "NOT"}

# Titles longer than this many words split into two lines at the midpoint
# ("around 7 words per line, double lines for long title names").
WRAP_AT_WORDS = 7

# ---------------------------------------------------------------- helpers

_TEXT_STYLE_KEYS = ("bold", "italic", "underline", "weightedFontFamily",
                    "fontSize", "foregroundColor", "backgroundColor")
_PARA_STYLE_KEYS = ("namedStyleType", "alignment", "indentStart",
                    "indentFirstLine", "spaceAbove", "spaceBelow", "lineSpacing")


def _export_lines(script_doc_id: str) -> list[str]:
    """The script's plain-text export — dropdown chips render their CURRENT
    item there (documents.get cannot see chip contents at all)."""
    try:
        raw = _api("GET", f"https://www.googleapis.com/drive/v3/files/{extract_doc_id(script_doc_id)}/export",
                   params={"mimeType": "text/plain"}, raw_response=True)
        return raw.decode("utf-8-sig", errors="replace").splitlines()
    except Exception:
        return []


def _anchor_name(script_doc_id: str) -> str:
    """The NL header's anchor chip current item, e.g. 'SANDRA H.'."""
    for ln in _export_lines(script_doc_id):
        m = re.search(r"NEWSLINE [^\n]*?-- ANCHOR:\s*([^\n]+)", ln)
        if m:
            return m.group(1).strip()
    return ""


def _eve_announcer(script_doc_id: str) -> str:
    """The EVE rundown's announcer chips (both sides of the '/') rendered as
    one line, e.g. 'ผปก. EVE / TikTok: @sandrahanutsaha'."""
    lines = _export_lines(script_doc_id)
    for i, ln in enumerate(lines):
        if "BRIEF EVENING" in ln:
            for j in range(i + 1, min(i + 8, len(lines))):
                if "ผู้ประกาศ" in lines[j]:
                    for k in range(j + 1, min(j + 3, len(lines))):
                        if lines[k].strip():
                            return lines[k].strip()
            break
    return ""


def _text(e: dict) -> str:
    return "".join(el.get("textRun", {}).get("content", "")
                   for el in e.get("paragraph", {}).get("elements", []))


def _runs(e: dict) -> list[tuple[str, dict]]:
    """(text, captured textStyle) runs — the style travels with the copy so
    the target inherits the source's look line by line."""
    out = []
    for el in e.get("paragraph", {}).get("elements", []):
        tr = el.get("textRun")
        if not tr:
            continue
        st = tr.get("textStyle", {}) or {}
        style = {k: st[k] for k in _TEXT_STYLE_KEYS if st.get(k) is not None}
        out.append((tr.get("content", ""), style))
    return out


def _para_style(e: dict) -> dict:
    ps = e.get("paragraph", {}).get("paragraphStyle", {}) or {}
    return {k: ps[k] for k in _PARA_STYLE_KEYS if ps.get(k) is not None}


def _style_name(e: dict) -> str:
    return e.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")


def _has_chip(e: dict) -> bool:
    """A dropdown/smart chip renders as a positioned element with NO textRun."""
    return any("textRun" not in el for el in e.get("paragraph", {}).get("elements", []))


def _upper_runs(runs: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    return [(t.upper(), s) for t, s in runs]


def _dominant_style(runs: list[tuple[str, dict]]) -> dict:
    for t, s in runs:
        if t.strip():
            return dict(s)
    return {}


def _strip_break_runs(runs: list[tuple[str, dict]] | None) -> list[tuple[str, dict]]:
    """Drop trailing newline-only runs — _write_para adds the paragraph break
    itself, so a captured one would double it."""
    out = list(runs or [])
    while out and not out[-1][0].strip():
        out = out[:-1]
    return out


def _table_cell_paras(e: dict) -> list[dict]:
    try:
        return [p for p in e["table"]["tableRows"][0]["tableCells"][0]["content"] if "paragraph" in p]
    except Exception:
        return []


def _table_text(e: dict) -> str:
    return "\n".join(_text(p).rstrip("\n") for p in _table_cell_paras(e)).strip()


def _table_cell(e: dict) -> list[dict]:
    """A 1x1 table's cell as copyable paragraphs: [{runs, para_style}, …]."""
    return [{"runs": _runs(p), "para_style": _para_style(p)} for p in _table_cell_paras(e)]


def _wrap_title_text(num: str, raw_text: str) -> str:
    """Transform title text: strip [tag], uppercase, and midpoint-wrap when > WRAP_AT_WORDS words."""
    text = re.sub(r"^\s*\d+\.\s*", "", raw_text)
    text = _TAG_RE.sub("", text).strip()
    words = text.split()
    if len(words) <= WRAP_AT_WORDS:
        return f"{num}. {text.upper()}"
    mid = (len(words) + 1) // 2
    while 1 < mid < len(words) - 1 and words[mid - 1].strip(",:;").upper() in _BREAK_STOPWORDS:
        mid -= 1
    line1 = f"{num}. {' '.join(words[:mid]).upper()}"
    line2 = " ".join(words[mid:]).upper()
    return f"{line1}\n{line2}"


def _wrap_title(num: str, runs: list[tuple[str, dict]]) -> list[dict]:
    """Title paragraph runs → one or TWO copyable para-blocks: tag-stripped,
    uppercased, midpoint-wrapped when longer than WRAP_AT_WORDS words (the
    break avoids landing right after a preposition/article). Style copied."""
    raw = "".join(t for t, _ in runs)
    style = _dominant_style(runs)
    wrapped = _wrap_title_text(num, raw)
    lines = wrapped.split("\n")
    return [{"kind": "para", "runs": [(line, style)], "para_style": None} for line in lines]


# ---------------------------------------------------------------- extraction

def _tab(doc: dict, needle: str) -> list[dict] | None:
    for t in doc.get("tabs", []):
        if needle.lower() in t.get("tabProperties", {}).get("title", "").lower():
            return t["documentTab"]["body"]["content"]
    return None


def _extract_rundown(els: list[dict]) -> dict:
    """The rundown LIST region of a tab → header + copyable blocks (styled)."""
    header = ""
    header_style: dict = {}
    header_runs: list[tuple[str, dict]] = []
    blocks: list[dict] = []
    in_sb = False
    started = False
    for e in els:
        if "paragraph" in e:
            raw = _text(e)
            txt = raw.strip()
            if _style_name(e) == "TITLE":
                header = txt
                header_style = _para_style(e)
                header_runs = _runs(e)  # colors/highlight travel with the copy
                started = True
                continue
            if not started or not txt:
                continue
            ps = _para_style(e)
            m = _NUM_RE.match(raw)
            if m:
                for b in _wrap_title(m.group(1), _runs(e)):
                    b["para_style"] = ps
                    blocks.append(b)
                in_sb = False
                continue
            if txt.startswith("ผู้ประกาศ") or re.match(r"^/+$", txt):
                blocks.append({"kind": "para", "runs": _runs(e), "para_style": ps})
                in_sb = False
                continue
            if re.match(r"^(SB|CG)\d*", txt):
                blocks.append({"kind": "para", "runs": _upper_runs(_runs(e)), "para_style": ps})
                in_sb = True
                continue
            if in_sb and raw[:1] == " " and not _THAI_RE.search(txt):
                blocks.append({"kind": "para", "runs": _upper_runs(_runs(e)), "para_style": ps})
                continue
            if _THAI_RE.search(txt):
                continue  # Thai summary lines under numbered titles
            blocks.append({"kind": "para", "runs": _upper_runs(_runs(e)), "para_style": ps})
        elif "table" in e and started:
            cell_text = _table_text(e)
            if "END CREDIT" in cell_text:
                blocks.append({"kind": "table", "cell": _table_cell(e)})
                break  # NL: rundown list ends at (and includes) the end-credit table
            if "โยนเบรค" in cell_text:
                break  # EVE: the throw-to-break table is not part of the rundown
            blocks.append({"kind": "table", "cell": _table_cell(e)})
    return {"header": header, "header_style": header_style, "header_runs": header_runs, "blocks": blocks}


def _extract_prompter(els: list[dict]) -> dict:
    """Story intro cells + show open / outro / ending for the PROMPTER doc
    (cells copied verbatim, formatting included)."""
    rows: list[dict] = []
    open_cell = None
    outro_cell = None
    ending_cell = None
    pending_story = False
    first_story_seen = False
    in_signoff = False
    for e in els:
        if "paragraph" in e:
            txt = _text(e).strip()
            if _style_name(e) == "HEADING_1":
                if _NUM_RE.match(txt):
                    pending_story = True
                    first_story_seen = True
                    in_signoff = False
                elif txt.startswith("ผู้ประกาศพูดลา"):
                    in_signoff = True
                    pending_story = False
        elif "table" in e:
            cell = _table_cell(e)
            if not first_story_seen:
                open_cell = cell  # last pre-story table = show-open cell
            elif pending_story:
                rows.append({"type": "intro", "cell": cell})
                pending_story = False
            elif in_signoff:
                if outro_cell is None:
                    outro_cell = cell
                    in_signoff = False
            else:
                # bare table after first story, not pending_story, not in_signoff
                rows.append({"type": "intermission", "cell": cell})

    if rows and rows[-1]["type"] == "intermission":
        ending_cell = rows.pop()["cell"]

    return {"open": open_cell, "rows": rows, "outro": outro_cell, "ending": ending_cell}


def extract_recording_docs(script_doc_id: str) -> dict:
    """Read-only extraction → everything needed for preview + apply."""
    doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{extract_doc_id(script_doc_id)}",
               params={"includeTabsContent": "true"})
    eve_els, nl_els = _tab(doc, "EVE"), _tab(doc, "NL RUNDOWN")
    errors: list[str] = []
    if not eve_els:
        errors.append("script doc has no 'NBTWB EVE RUNDOWN' tab")
    if not nl_els:
        errors.append("script doc has no 'NL RUNDOWN' tab")
    if errors:
        return {"errors": errors}

    eve = _extract_rundown(eve_els)
    nl = _extract_rundown(nl_els)
    eve_p = _extract_prompter(eve_els)
    nl_p = _extract_prompter(nl_els)

    m = _DATE_RE.search(eve.get("header", ""))
    date_ddmmyy = date_iso = ""
    if m and m.group(2) in _MONTHS:
        d, mo, y = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
        date_iso = f"{y:04d}-{mo:02d}-{d:02d}"
        date_ddmmyy = f"{d:02d}{mo:02d}{str(y)[-2:]}"
    else:
        errors.append(f"no date found in EVE header {eve.get('header')!r}")
    if not any(r["type"] == "intro" for r in eve_p["rows"]):
        errors.append("EVE: no story intro cells found")
    if not any(r["type"] == "intro" for r in nl_p["rows"]):
        errors.append("NL: no story intro cells found")
    if nl_p["open"] is None:
        errors.append("NL: show-open cell not found")
    if eve_p["outro"] is None:
        errors.append("EVE: outro cell not found")
    if nl_p["ending"] is None:
        errors.append("NL: ending cell not found")
    warnings: list[str] = []
    anchor_name = _anchor_name(script_doc_id)
    if not anchor_name:
        warnings.append("NL header has no anchor name after 'ANCHOR:' — the script's title is blank; fill it in the RUNDOWN doc by hand")
    eve_announcer = _eve_announcer(script_doc_id)
    if not eve_announcer:
        warnings.append("EVE announcer chips render empty — fill the announcer line by hand")

    rundown_name = f"RUNDOWN NL-NWB {date_ddmmyy}" if date_ddmmyy else ""
    prompter_name = f"PROMPTER NL-NWB {date_ddmmyy}" if date_ddmmyy else ""
    return {"script_doc": extract_doc_id(script_doc_id), "date": date_iso,
            "rundown_name": rundown_name, "prompter_name": prompter_name,
            "anchor_name": anchor_name, "eve_announcer": eve_announcer,
            "eve": eve, "nl": nl, "prompter_eve": eve_p, "prompter_nl": nl_p,
            "errors": errors, "warnings": warnings}


def _row_heads(cells: list[list[dict]]) -> list[str]:
    out = []
    for paras in cells:
        out.append(("".join(t for p in paras for t, _ in p["runs"]))[:80] or "(empty)")
    return out


def _cell_text(cell: list[dict]) -> str:
    return "\n".join("".join(t for t, _ in p["runs"]) for p in cell).strip()


def preview_recording_docs(script_doc_id: str, rundown_doc_id: str | None = None,
                           prompter_doc_id: str | None = None) -> dict:
    data = extract_recording_docs(script_doc_id)
    if not data.get("eve"):
        return {"errors": data.get("errors", ["extraction failed"])}
    eve_p, nl_p = data["prompter_eve"], data["prompter_nl"]
    eve_cells = [r["cell"] for r in eve_p["rows"]] + ([eve_p["outro"]] if eve_p["outro"] else [])
    nl_cells = (
        ([nl_p["open"]] if nl_p["open"] else [])
        + [r["cell"] for r in nl_p["rows"]]
        + ([nl_p["ending"]] if nl_p["ending"] else [])
    )
    r_name = data.get("rundown_name", "")
    p_name = data.get("prompter_name", "")
    target_names = [n for n in (r_name, p_name) if n]
    return {
        "script_doc": data["script_doc"],
        "date": data["date"],
        "rundown_name": r_name,
        "prompter_name": p_name,
        "target_names": target_names,
        "anchor_name": data.get("anchor_name", ""),
        "eve_announcer": data.get("eve_announcer", ""),
        "eve": {"header": data["eve"]["header"],
                "lines": ["".join(t for t, _ in b["runs"]) for b in data["eve"]["blocks"] if b["kind"] == "para"],
                "tables": [_cell_text(b["cell"])[:40] for b in data["eve"]["blocks"] if b["kind"] == "table"]},
        "nl": {"header": data["nl"]["header"],
               "lines": ["".join(t for t, _ in b["runs"]) for b in data["nl"]["blocks"] if b["kind"] == "para"],
               "tables": [_cell_text(b["cell"])[:40] for b in data["nl"]["blocks"] if b["kind"] == "table"]},
        "prompter": {
            "eve_rows": _row_heads(eve_cells),
            "nl_rows": _row_heads(nl_cells),
        },
        "errors": data["errors"],
        "warnings": data.get("warnings", []),
    }


# ---------------------------------------------------------------- apply (forward writer)

def _block_text(b: dict) -> str:
    return "".join(t for t, _ in b.get("runs", []))


def _is_num_title(b: dict) -> bool:
    if b.get("kind") != "para":
        return False
    return bool(_NUM_RE.match(_block_text(b)))


def _is_sb_cg_start(b: dict) -> bool:
    if b.get("kind") != "para":
        return False
    return bool(re.match(r"^(SB|CG)\d*", _block_text(b).strip()))


def _is_presenter(b: dict) -> bool:
    if b.get("kind") != "para":
        return False
    txt = _block_text(b).strip()
    return txt.startswith("ผู้ประกาศ") or bool(re.match(r"^/+$", txt))


def _space_rundown_blocks(blocks: list[dict]) -> list[dict]:
    """Insert a blank paragraph block between rundown items (after a numbered
    title's last line or after an SB/CG continuation group). Do not add blanks
    around marker tables."""
    spaced: list[dict] = []
    blank = {"kind": "para", "runs": [(" ", {})], "para_style": None}
    n = len(blocks)
    in_sb = False
    for i, b in enumerate(blocks):
        spaced.append(b)
        if b.get("kind") == "table":
            in_sb = False
            continue
        if _is_presenter(b):
            in_sb = False
            continue

        next_b = blocks[i + 1] if i + 1 < n else None
        prev_b = blocks[i - 1] if i > 0 else None

        # Check if b is a numbered title
        if _is_num_title(b):
            in_sb = False
            # Check if next block is wrapped line 2 of this title
            is_line2 = (
                next_b is not None
                and next_b.get("kind") == "para"
                and not _is_num_title(next_b)
                and not _is_sb_cg_start(next_b)
                and not _is_presenter(next_b)
                and not _block_text(next_b).startswith(" ")
            )
            if is_line2:
                continue
            # If followed by SB/CG, item continues into SB/CG
            if next_b and _is_sb_cg_start(next_b):
                continue
            # Otherwise, 1-line title without SB/CG ends here
            if next_b is None or next_b.get("kind") != "table":
                spaced.append(dict(blank))
            continue

        # Check if b is wrapped line 2 of a numbered title
        is_prev_num = prev_b is not None and _is_num_title(prev_b)
        if is_prev_num and not _is_sb_cg_start(b) and not _block_text(b).startswith(" "):
            in_sb = False
            # If followed by SB/CG, item continues
            if next_b and _is_sb_cg_start(next_b):
                continue
            if next_b is None or next_b.get("kind") != "table":
                spaced.append(dict(blank))
            continue

        # Check if b starts or continues an SB/CG group
        if _is_sb_cg_start(b):
            in_sb = True
        elif in_sb and _block_text(b).startswith(" ") and not _THAI_RE.search(_block_text(b)):
            pass
        else:
            in_sb = False
            continue

        # If we are in SB/CG, check if next block continues SB/CG
        next_is_sb = (
            next_b is not None
            and next_b.get("kind") == "para"
            and (
                _is_sb_cg_start(next_b)
                or (in_sb and _block_text(next_b).startswith(" ") and not _THAI_RE.search(_block_text(next_b)))
            )
        )
        if not next_is_sb:
            in_sb = False
            if next_b is None or next_b.get("kind") != "table":
                spaced.append(dict(blank))

    return spaced


def _batch(doc_id: str, requests: list[dict]) -> dict:
    """batchUpdate with 429 backoff."""
    import time as _time
    for attempt in range(4):
        try:
            return _api("POST", f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
                        body={"requests": requests})
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                _time.sleep(25 * (attempt + 1))
                continue
            raise


# ---------------------------------------------------------------- pure edit plans (for tests & batching)

def _classify_prompter_tables(els: list[dict], is_eve: bool = False) -> list[tuple[dict, str]]:
    """Classify tables in a tab into roles: 'open', 'intro', 'intermission', 'outro', 'ending', 'drop'."""
    classified: list[tuple[dict, str]] = []
    pre_story_tables: list[dict] = []
    first_story_seen = False
    pending_story = False
    in_signoff = False
    outro_seen = False

    for e in els:
        if "paragraph" in e:
            txt = _text(e).strip()
            st = _style_name(e)
            if st == "HEADING_1":
                if _NUM_RE.match(txt):
                    pending_story = True
                    first_story_seen = True
                    in_signoff = False
                elif txt.startswith("ผู้ประกาศพูดลา"):
                    in_signoff = True
                    pending_story = False
        elif "table" in e:
            if not first_story_seen:
                pre_story_tables.append(e)
            elif pending_story:
                classified.append((e, "intro"))
                pending_story = False
            elif in_signoff:
                if not outro_seen:
                    classified.append((e, "outro"))
                    outro_seen = True
                    in_signoff = False
                else:
                    classified.append((e, "drop"))
            else:
                # Bare table between stories or after stories
                classified.append((e, "intermission"))

    # Handle pre-story tables:
    # In NL: the last pre-story table is "open", all earlier ones are "drop"
    # In EVE: pre-story tables (like jingle) are "drop"
    if pre_story_tables:
        if is_eve:
            for t in pre_story_tables:
                classified.insert(0, (t, "drop"))
        else:
            open_table = pre_story_tables[-1]
            earlier = pre_story_tables[:-1]
            for t in reversed(earlier):
                classified.insert(0, (t, "drop"))
            classified.insert(len(earlier), (open_table, "open"))

    # Handle trailing intermission table:
    # If the last non-drop table is "intermission", in NL it's the "ending" table
    non_drop = [x for x in classified if x[1] != "drop"]
    if non_drop and non_drop[-1][1] == "intermission":
        last_t, _ = non_drop[-1]
        role = "ending" if not is_eve else "intermission"
        for idx in range(len(classified) - 1, -1, -1):
            if classified[idx][0] is last_t:
                classified[idx] = (last_t, role)
                break

    return classified


def _rundown_edit_requests(
    tab_elements: list[dict],
    anchor_name: str = "",
    tab_id: str | None = None,
    is_nl: bool = False,
) -> list[dict]:
    """Build batchUpdate requests for a single rundown tab snapshot.

    Ranges are RAW snapshot indexes emitted in DESCENDING start order (with a
    same-start delete before its insert): an edit at a higher index never
    shifts a lower one, so every request in the one batch stays valid — no
    delta arithmetic to get wrong."""
    if not tab_elements:
        return []

    ops: list[tuple[int, int, str, str]] = []  # (start, end, kind, text)

    if not is_nl:
        for e in tab_elements:
            if "paragraph" in e:
                t = _text(e)
                if "-- ANCHOR:" in t or "NEWSLINE" in t:
                    is_nl = True
                    break

    has_title = any("paragraph" in e and _style_name(e) == "TITLE" for e in tab_elements)
    started = not has_title
    in_sb = False
    end_table_idx: int | None = None

    def add_delete(start: int, end: int):
        if end > start:
            ops.append((start, end, "delete", ""))

    def add_insert(pos: int, text: str):
        if text:
            ops.append((pos, pos, "insert", text))

    def add_replace(start: int, end: int, new_text: str):
        if end > start:
            ops.append((start, end, "delete", ""))
        if new_text:
            ops.append((start, start, "insert", new_text))

    for i, e in enumerate(tab_elements):
        if "paragraph" in e:
            raw = _text(e)
            txt = raw.strip()
            style = _style_name(e)

            if style == "TITLE":
                started = True
                if is_nl and anchor_name and not _has_chip(e):
                    # chips render their own current item — insert the anchor
                    # name as text only when the dropdown chip is absent
                    m = re.search(r"-- ANCHOR:[ \t]*", raw)
                    if m:
                        to_insert = anchor_name.strip() if m.group(0).endswith(" ") else f" {anchor_name.strip()}"
                        add_insert(e["startIndex"] + m.end(), to_insert)
                continue

            if not started or not txt:
                continue

            if end_table_idx is not None:
                continue

            # Numbered title paragraph
            m = _NUM_RE.match(raw)
            if m:
                in_sb = False
                num = m.group(1)
                new_text = _wrap_title_text(num, raw)
                old_start = e["startIndex"]
                old_end = e["endIndex"] - 1
                add_replace(old_start, old_end, new_text)
                continue

            # Presenter lines (chips) - keep as-is
            if txt.startswith("ผู้ประกาศ") or re.match(r"^/+$", txt):
                in_sb = False
                continue

            # SB/CG start line
            if re.match(r"^(SB|CG)\d*", txt):
                in_sb = True
                old_text = raw.rstrip("\r\n")
                new_text = old_text.upper()
                if new_text != old_text:
                    add_replace(e["startIndex"], e["startIndex"] + len(old_text), new_text)
                continue

            # SB/CG continuation line
            if in_sb and raw[:1] == " " and not _THAI_RE.search(txt):
                old_text = raw.rstrip("\r\n")
                new_text = old_text.upper()
                if new_text != old_text:
                    add_replace(e["startIndex"], e["startIndex"] + len(old_text), new_text)
                continue

            # Thai summary lines - delete whole paragraph
            if _THAI_RE.search(txt):
                # keep the paragraph mark (endIndex-1): deleting a mark
                # adjacent to a table is forbidden, and the empty line left
                # behind is the rundown's own spacing
                add_delete(e["startIndex"], e["endIndex"] - 1)
                continue

            # Any other rundown text line
            old_text = raw.rstrip("\r\n")
            new_text = old_text.upper()
            if new_text != old_text:
                add_replace(e["startIndex"], e["startIndex"] + len(old_text), new_text)

        elif "table" in e and started and end_table_idx is None:
            cell_text = _table_text(e)
            if (is_nl and "END CREDIT" in cell_text) or (not is_nl and "โยนเบรค" in cell_text):
                end_table_idx = i

    # Trim everything after the boundary marker table (stopping at the ⚓
    # ANCHORS heading when present) — per-element deletes; a paragraph mark
    # immediately before a table must survive (API rule), so those deletes
    # keep the mark (endIndex-1), leaving a blank line.
    if end_table_idx is not None and end_table_idx + 1 < len(tab_elements):
        doomed = []
        for e in tab_elements[end_table_idx + 1:]:
            if "paragraph" in e:
                t = _text(e).strip()
                st = _style_name(e)
                if (t.startswith("⚓") or t.upper().startswith("ANCHOR")) and st.startswith("HEADING"):
                    break
            doomed.append(e)
        for j, e in enumerate(doomed):
            nxt = doomed[j + 1] if j + 1 < len(doomed) else None
            before_table = nxt is not None and "table" in nxt
            last_of_tab = e is tab_elements[-1]
            end = e["endIndex"] - 1 if (before_table or last_of_tab) else e["endIndex"]
            add_delete(e["startIndex"], end)

    reqs: list[dict] = []
    for start, end, kind, text in sorted(
            ops, key=lambda o: (-o[0], 0 if o[2] == "delete" else 1)):
        if kind == "delete":
            rg: dict = {"startIndex": start, "endIndex": end}
            if tab_id:
                rg["tabId"] = tab_id
            reqs.append({"deleteContentRange": {"range": rg}})
        else:
            loc: dict = {"index": start}
            if tab_id:
                loc["tabId"] = tab_id
            reqs.append({"insertText": {"location": loc, "text": text}})
    return reqs


def _prompter_edit_requests(
    tab_elements: list[dict],
    table_roles: list[tuple[dict, str]] | list[dict] | dict,
    tab_id: str | None = None,
) -> list[dict]:
    """Build batchUpdate delete requests for a single prompter tab snapshot.
    Gap ranges are RAW snapshot indexes emitted in DESCENDING order so no
    delete shifts a later one's content (no delta arithmetic)."""
    if not tab_elements:
        return []

    kept_tables = []
    if isinstance(table_roles, list):
        for item in table_roles:
            if isinstance(item, tuple) and len(item) == 2:
                t, role = item
                if role in {"open", "intro", "intermission", "outro", "ending"}:
                    kept_tables.append(t)
            elif isinstance(item, dict):
                role = item.get("role", "intro")
                t = item.get("element", item)
                if role != "drop":
                    kept_tables.append(t)
    elif isinstance(table_roles, dict):
        for e in tab_elements:
            if "table" in e:
                role = table_roles.get(e.get("startIndex")) or table_roles.get(id(e))
                if role in {"open", "intro", "intermission", "outro", "ending"}:
                    kept_tables.append(e)

    kept_tables = sorted(kept_tables, key=lambda t: t.get("startIndex", 0))

    ranges: list[tuple[int, int]] = []

    def add_delete(start: int, end: int):
        if end > start:
            ranges.append((start, end))

    tab_start = tab_elements[0].get("startIndex", 1)
    tab_end = tab_elements[-1].get("endIndex", 1) - 1

    if not kept_tables:
        if tab_end > tab_start:
            add_delete(tab_start, tab_end)
        return _emit_deletes(ranges, tab_id)

    # Gap before first kept table (never delete the mark right before it)
    if kept_tables[0]["startIndex"] > tab_start:
        add_delete(tab_start, kept_tables[0]["startIndex"] - 1)

    # Gaps between kept tables (shrink both ends: keep the marks after and
    # before the tables; leftover marks read as the row separators)
    for i in range(len(kept_tables) - 1):
        gap_start = kept_tables[i]["endIndex"] + 1
        gap_end = kept_tables[i + 1]["startIndex"] - 1
        if gap_end > gap_start:
            add_delete(gap_start, gap_end)

    # Gap after last kept table
    last_end = kept_tables[-1]["endIndex"]
    if tab_end > last_end:
        add_delete(last_end + 1, tab_end)

    return _emit_deletes(ranges, tab_id)


def _emit_deletes(ranges: list[tuple[int, int]], tab_id: str | None) -> list[dict]:
    """Delete requests in DESCENDING start order — raw snapshot ranges stay
    valid because a higher delete never shifts lower content."""
    reqs: list[dict] = []
    for start, end in sorted(ranges, key=lambda r: -r[0]):
        rg: dict = {"startIndex": start, "endIndex": end}
        if tab_id:
            rg["tabId"] = tab_id
        reqs.append({"deleteContentRange": {"range": rg}})
    return reqs


# ---------------------------------------------------------------- copy-based apply

def _ensure_copy(src_doc_id: str, dest_name: str, folder_id: str) -> tuple[str, str]:
    """Idempotently copy src_doc_id to dest_name in folder_id: trash old copy if exists, then copy fresh."""
    clean_src = extract_doc_id(src_doc_id)
    # Check for existing file with same name in folder
    q = f"name = '{dest_name}' and '{folder_id}' in parents and trashed = false"
    res = _api(
        "GET",
        "https://www.googleapis.com/drive/v3/files",
        params={
            "q": q,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": "files(id, name)",
        },
    )
    for f in res.get("files", []):
        _api(
            "PATCH",
            f"https://www.googleapis.com/drive/v3/files/{f['id']}",
            params={"supportsAllDrives": "true"},
            body={"trashed": True},
        )
    # Copy fresh
    copy_res = _api(
        "POST",
        f"https://www.googleapis.com/drive/v3/files/{clean_src}/copy",
        params={
            "addParents": folder_id,
            "supportsAllDrives": "true",
            "fields": "id,name,webViewLink",
        },
        body={"name": dest_name},
    )
    new_id = copy_res.get("id", "")
    url = f"https://docs.google.com/document/d/{new_id}/edit"
    return new_id, url


def apply_recording_docs(script_doc_id: str, rundown_doc_id: str | None = None,
                         prompter_doc_id: str | None = None) -> dict:
    """Copy-based apply: duplicate script doc to RUNDOWN + PROMPTER and trim in-place."""
    data = extract_recording_docs(script_doc_id)
    fatal = [e for e in data.get("errors", []) if "no " in e]
    if fatal:
        return {"applied": False, "errors": fatal}

    rundown_name = data.get("rundown_name", "")
    prompter_name = data.get("prompter_name", "")
    anchor_name = data.get("anchor_name", "")

    # 1. RUNDOWN copy & trim
    rundown_id, rundown_url = _ensure_copy(script_doc_id, rundown_name, DEST_FOLDER)
    rd_doc = _api(
        "GET",
        f"https://docs.googleapis.com/v1/documents/{rundown_id}",
        params={"includeTabsContent": "true"},
    )
    rd_reqs: list[dict] = []
    tabs = rd_doc.get("tabs", [])
    if tabs:
        for tab in tabs:
            tab_prop = tab.get("tabProperties", {})
            title = tab_prop.get("title", "")
            tab_id = tab_prop.get("tabId")
            body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])
            if "EVE" in title.upper():
                reqs = _rundown_edit_requests(body_content, anchor_name="", tab_id=tab_id, is_nl=False)
                rd_reqs.extend(reqs)
            elif "NL" in title.upper() and "RUNDOWN" in title.upper():
                reqs = _rundown_edit_requests(body_content, anchor_name=anchor_name, tab_id=tab_id, is_nl=True)
                rd_reqs.extend(reqs)
    else:
        body_content = rd_doc.get("body", {}).get("content", [])
        rd_reqs.extend(_rundown_edit_requests(body_content, anchor_name=anchor_name, tab_id=None, is_nl=True))

    if rd_reqs:
        _batch(rundown_id, rd_reqs)

    # 2. PROMPTER copy & trim
    prompter_id, prompter_url = _ensure_copy(script_doc_id, prompter_name, DEST_FOLDER)
    p_doc = _api(
        "GET",
        f"https://docs.googleapis.com/v1/documents/{prompter_id}",
        params={"includeTabsContent": "true"},
    )
    p_reqs: list[dict] = []
    ptabs = p_doc.get("tabs", [])
    if ptabs:
        for tab in ptabs:
            tab_prop = tab.get("tabProperties", {})
            title = tab_prop.get("title", "")
            tab_id = tab_prop.get("tabId")
            body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])
            if "EVE" in title.upper():
                roles = _classify_prompter_tables(body_content, is_eve=True)
                reqs = _prompter_edit_requests(body_content, roles, tab_id=tab_id)
                p_reqs.extend(reqs)
            elif "NL" in title.upper() and "RUNDOWN" in title.upper():
                roles = _classify_prompter_tables(body_content, is_eve=False)
                reqs = _prompter_edit_requests(body_content, roles, tab_id=tab_id)
                p_reqs.extend(reqs)
    else:
        body_content = p_doc.get("body", {}).get("content", [])
        roles = _classify_prompter_tables(body_content, is_eve=False)
        p_reqs.extend(_prompter_edit_requests(body_content, roles, tab_id=None))

    if p_reqs:
        _batch(prompter_id, p_reqs)

    return {
        "applied": True,
        "rundown_doc": rundown_id,
        "prompter_doc": prompter_id,
        "rundown_url": rundown_url,
        "prompter_url": prompter_url,
        "rundown_name": rundown_name,
        "prompter_name": prompter_name,
        "errors": data.get("errors", []),
        "warnings": data.get("warnings", []),
    }


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> None:
    import argparse
    if not argv or argv[0] not in ("preview", "apply"):
        print(json.dumps({"error": "verb required: preview | apply"}, ensure_ascii=False))
        sys.exit(2)
    p = argparse.ArgumentParser(description="RECORDING DOCS builder")
    p.add_argument("--script-doc", required=True, help="Daily script Doc ID or URL")
    p.add_argument("--rundown-doc", default=None, help="RUNDOWN Doc ID (legacy/optional)")
    p.add_argument("--prompter-doc", default=None, help="PROMPTER Doc ID (legacy/optional)")
    args = p.parse_args(argv[1:])
    fn = apply_recording_docs if argv[0] == "apply" else preview_recording_docs
    print(json.dumps(fn(args.script_doc, args.rundown_doc, args.prompter_doc), ensure_ascii=False))



if __name__ == "__main__":
    main(sys.argv[1:])
