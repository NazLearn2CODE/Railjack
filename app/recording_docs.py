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

# The newsroom's standing recording docs (defaults; editable per-run).
DEFAULT_RUNDOWN_DOC = "1_aX1eJ9eOBEPAl6ojTqM1HwAa7jF_8GtjH1CEvqrGAY"
DEFAULT_PROMPTER_DOC = "13XiTeaPqicwbexYwRukEfWWse5krxTZriJ18Ut61rSg"

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
                    "fontSize", "foregroundColor")
_PARA_STYLE_KEYS = ("namedStyleType", "alignment", "indentStart",
                    "indentFirstLine", "spaceAbove", "spaceBelow", "lineSpacing")


def _anchor_name(script_doc_id: str) -> str:
    """The script's NL header dropdown chip renders its current item in the
    Drive plain-text export (documents.get cannot see chip contents)."""
    try:
        raw = _api("GET", f"https://www.googleapis.com/drive/v3/files/{extract_doc_id(script_doc_id)}/export",
                   params={"mimeType": "text/plain"}, raw_response=True)
        text = raw.decode("utf-8-sig", errors="replace")
        m = re.search(r"NEWSLINE [^\n]*?-- ANCHOR:\s*([^\n]+)", text)
        return m.group(1).strip() if m else ""
    except Exception:
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


def _upper_runs(runs: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    return [(t.upper(), s) for t, s in runs]


def _dominant_style(runs: list[tuple[str, dict]]) -> dict:
    for t, s in runs:
        if t.strip():
            return dict(s)
    return {}


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


def _wrap_title(num: str, runs: list[tuple[str, dict]]) -> list[dict]:
    """Title paragraph runs → one or TWO copyable para-blocks: tag-stripped,
    uppercased, midpoint-wrapped when longer than WRAP_AT_WORDS words (the
    break avoids landing right after a preposition/article). Style copied."""
    text = re.sub(r"^\s*\d+\.\s*", "", "".join(t for t, _ in runs))  # number comes from _NUM_RE
    text = _TAG_RE.sub("", text).strip()
    style = _dominant_style(runs)
    words = text.split()
    if len(words) <= WRAP_AT_WORDS:
        return [{"kind": "para", "runs": [(f"{num}. {text.upper()}", style)],
                 "para_style": None}]  # para_style filled by caller
    mid = (len(words) + 1) // 2
    while 1 < mid < len(words) - 1 and words[mid - 1].strip(",:;").upper() in _BREAK_STOPWORDS:
        mid -= 1
    return [{"kind": "para", "runs": [(f"{num}. {' '.join(words[:mid]).upper()}", style)], "para_style": None},
            {"kind": "para", "runs": [(" ".join(words[mid:]).upper(), style)], "para_style": None}]


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
    return {"header": header, "header_style": header_style, "blocks": blocks}


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

    return {"script_doc": extract_doc_id(script_doc_id), "date": date_iso,
            "rundown_name": f"RUNDOWN NL-NWB {date_ddmmyy}" if date_ddmmyy else "",
            "anchor_name": anchor_name,
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
    return {
        "script_doc": data["script_doc"],
        "date": data["date"],
        "rundown_name": data["rundown_name"],
        "anchor_name": data.get("anchor_name", ""),
        "rundown_doc": extract_doc_id(rundown_doc_id) if rundown_doc_id else DEFAULT_RUNDOWN_DOC,
        "prompter_doc": extract_doc_id(prompter_doc_id) if prompter_doc_id else DEFAULT_PROMPTER_DOC,
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
    """batchUpdate with 429 backoff — a full apply is write-heavy (one batch
    per paragraph/table) and the per-minute write quota bites."""
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


def _write_para(doc_id: str, at: int, runs: list[tuple[str, dict]], para_style: dict | None) -> int:
    """One paragraph (runs joined, trailing newline) inserted at ``at``; styles
    applied per run + paragraph style, all in one batch. Returns index shift."""
    text = "".join(t for t, _ in runs) or " "
    req: list[dict] = [{"insertText": {"location": {"index": at}, "text": text + "\n"}}]
    pos = at
    for t, s in runs:
        if t and s:
            req.append({"updateTextStyle": {
                "range": {"startIndex": pos, "endIndex": pos + len(t)},
                "textStyle": s, "fields": ",".join(sorted(s))}})
        pos += len(t)
    if para_style:
        req.append({"updateParagraphStyle": {
            "range": {"startIndex": at, "endIndex": at + len(text)},
            "paragraphStyle": para_style, "fields": ",".join(sorted(para_style))}})
    _batch(doc_id, req)
    return len(text) + 1


def _write_table(doc_id: str, at: int, cell: list[dict]) -> int:
    """One 1x1 table inserted at ``at``, its cell filled with copied paragraphs
    (each styled run re-applied; paragraphs separated by newlines). Returns
    the total index shift (structure + cell text)."""
    _batch(doc_id, [{"insertTable": {"rows": 1, "columns": 1, "location": {"index": at}}}])
    doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}")
    # the cursor advances forward, so the newest table is the highest-indexed one
    t_el = max((e for e in doc["body"]["content"] if "table" in e),
               key=lambda e: e.get("startIndex") or 0)
    span = t_el["endIndex"] - t_el["startIndex"]
    pos = t_el["table"]["tableRows"][0]["tableCells"][0]["content"][0]["startIndex"]
    inserted = 0
    for i, p in enumerate(cell):
        text = "".join(t for t, _ in p["runs"])
        if not text:
            continue
        if i > 0:  # paragraph break before this cell paragraph
            _batch(doc_id, [{"insertText": {"location": {"index": pos}, "text": "\n"}}])
            pos += 1
            inserted += 1
        req: list[dict] = [{"insertText": {"location": {"index": pos}, "text": text}}]
        rpos = pos
        for t, s in p["runs"]:
            if t and s:
                req.append({"updateTextStyle": {
                    "range": {"startIndex": rpos, "endIndex": rpos + len(t)},
                    "textStyle": s, "fields": ",".join(sorted(s))}})
            rpos += len(t)
        if p.get("para_style"):
            req.append({"updateParagraphStyle": {
                "range": {"startIndex": pos, "endIndex": pos + len(text)},
                "paragraphStyle": p["para_style"], "fields": ",".join(sorted(p["para_style"]))}})
        _batch(doc_id, req)
        pos += len(text)
        inserted += len(text)
    return span + inserted


def _write_blocks(doc_id: str, seq: list[dict], keep_anchor: bool = False) -> int:
    """Replace a doc's body (or everything above the Anchor heading when
    ``keep_anchor``) with ``seq``, appending FORWARD at one fixed index — the
    start of the preserved tail paragraph (the Anchor heading, or the doc's
    final paragraph for a full replace). Every insert lands right before that
    tail, i.e. after everything inserted before it. Returns tables written."""
    doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}")
    if keep_anchor:
        tail = next((e for e in doc["body"]["content"]
                     if "paragraph" in e and _text(e).strip() == "Anchor"
                     and _style_name(e).startswith("HEADING")), None)
        if tail is None:
            raise RuntimeError("RUNDOWN doc has no 'Anchor' heading to preserve")
    else:
        tail = doc["body"]["content"][-1]
    # whole-element deletion, highest index first in one batch (earlier ranges
    # stay valid as later ones apply) — never splits a table or paragraph
    doomed = [e for e in doc["body"]["content"]
              if e.get("startIndex") is not None and e["startIndex"] < tail["startIndex"]
              and "sectionBreak" not in e]
    if doomed:
        _batch(doc_id, [
            {"deleteContentRange": {"range": {"startIndex": e["startIndex"], "endIndex": e["endIndex"]}}}
            for e in reversed(doomed)])
    def _tail_at() -> int:
        """Current insert point: right before the preserved tail paragraph.
        Refetched every block — Docs auto-inserts separator paragraphs around
        tables, so cursor arithmetic cannot be trusted."""
        doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}")
        if keep_anchor:
            return next(e["startIndex"] for e in doc["body"]["content"]
                        if "paragraph" in e and _text(e).strip() == "Anchor")
        return doc["body"]["content"][-1]["startIndex"]

    tables = 0
    for b in seq:
        at = _tail_at()
        if b["kind"] in ("table", "runs"):
            _write_table(doc_id, at, b["cell"])
            tables += 1
        else:
            _write_para(doc_id, at, b["runs"], b.get("para_style"))
    _cleanup(doc_id, keep_anchor)
    return tables


def _cleanup(doc_id: str, keep_anchor: bool) -> None:
    """Insertion at the preserved tail's boundary bleeds its style (HEADING_1
    for the Anchor block) onto auto-inserted separator paragraphs. The empty
    paragraphs themselves are welcome spacing (the hand-made docs had them
    too) — only their style needs resetting. No deletions: paragraph deletes
    near table boundaries are restricted by the API."""
    doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}")
    if keep_anchor:
        tail_start = next((e["startIndex"] for e in doc["body"]["content"]
                           if "paragraph" in e and _text(e).strip() == "Anchor"), None)
        if tail_start is None:
            return
    else:
        tail_start = doc["body"]["content"][-1]["startIndex"]
    bleed = [e for e in doc["body"]["content"]
             if e.get("startIndex") is not None and e["startIndex"] < tail_start
             and "paragraph" in e and _style_name(e).startswith("HEADING")
             and _style_name(e) != "TITLE"]
    if bleed:
        _batch(doc_id, [
            {"updateParagraphStyle": {
                "range": {"startIndex": e["startIndex"], "endIndex": max(e["startIndex"] + 1, e["endIndex"] - 1)},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}}
            for e in bleed])
    # collapse consecutive blank paragraphs to one (auto-separators + our
    # spacers double up); paragraph-to-paragraph deletes are boundary-safe
    extra: list[dict] = []
    prev_blank = False
    for e in doc["body"]["content"]:
        is_blank_para = "paragraph" in e and e.get("startIndex") is not None \
            and e["startIndex"] < tail_start and not _text(e).strip()
        if is_blank_para:
            if prev_blank:
                extra.append(e)
            prev_blank = True
        else:
            prev_blank = False
    if extra:
        _batch(doc_id, [
            {"deleteContentRange": {"range": {"startIndex": e["startIndex"], "endIndex": e["endIndex"]}}}
            for e in reversed(extra)])


def apply_recording_docs(script_doc_id: str, rundown_doc_id: str | None = None,
                         prompter_doc_id: str | None = None) -> dict:
    data = extract_recording_docs(script_doc_id)
    fatal = [e for e in data.get("errors", []) if "no " in e]
    if fatal:
        return {"applied": False, "errors": fatal}
    rundown_id = extract_doc_id(rundown_doc_id) if rundown_doc_id else DEFAULT_RUNDOWN_DOC
    prompter_id = extract_doc_id(prompter_doc_id) if prompter_doc_id else DEFAULT_PROMPTER_DOC
    out = {"applied": True, "rundown_doc": rundown_id, "prompter_doc": prompter_id,
           "rundown_name": data["rundown_name"], "errors": data.get("errors", []),
           "warnings": data.get("warnings", [])}

    # ── RUNDOWN doc: rename, then replace everything above the Anchor block ──
    if data["rundown_name"]:
        cur = _api("GET", f"https://www.googleapis.com/drive/v3/files/{rundown_id}",
                   params={"fields": "name"})
        if cur.get("name") != data["rundown_name"]:
            _api("PATCH", f"https://www.googleapis.com/drive/v3/files/{rundown_id}",
                 body={"name": data["rundown_name"]})
            out["renamed_to"] = data["rundown_name"]
    seq: list[dict] = [{"kind": "para", "runs": [(data["eve"]["header"],
                                                  _header_text_style(data))],
                        "para_style": data["eve"].get("header_style") or {"namedStyleType": "TITLE"}}]
    seq += _space_rundown_blocks(data["eve"]["blocks"])
    nl_head_text = data["nl"]["header"] + (f" {data['anchor_name']}" if data.get("anchor_name") else "")
    seq += [{"kind": "para", "runs": [(nl_head_text, _header_text_style(data))],
             "para_style": data["nl"].get("header_style") or {"namedStyleType": "TITLE"}}]
    seq += _space_rundown_blocks(data["nl"]["blocks"])
    seq += [{"kind": "para", "runs": [(" ", {})], "para_style": None}]  # spacer
    try:
        out["rundown_tables"] = _write_blocks(rundown_id, seq, keep_anchor=True)
    except RuntimeError as e:
        return {"applied": False, "errors": [str(e)]}

    # ── PROMPTER doc: full replace ──
    pseq: list[dict] = [{"kind": "runs", "cell": r["cell"]} for r in data["prompter_eve"]["rows"]]
    if data["prompter_eve"]["outro"]:
        pseq.append({"kind": "runs", "cell": data["prompter_eve"]["outro"]})
    pseq.append({"kind": "para", "runs": [("++++", {"bold": True})], "para_style": None})
    if data["prompter_nl"]["open"]:
        pseq.append({"kind": "runs", "cell": data["prompter_nl"]["open"]})
    pseq += [{"kind": "runs", "cell": r["cell"]} for r in data["prompter_nl"]["rows"]]
    if data["prompter_nl"]["ending"]:
        pseq.append({"kind": "runs", "cell": data["prompter_nl"]["ending"]})
    out["prompter_tables"] = _write_blocks(prompter_id, pseq)
    return out


def _header_text_style(data: dict) -> dict:
    """The show headers read Tahoma 11 bold (Naz 2026-08-18)."""
    return {"bold": True, "weightedFontFamily": {"fontFamily": "Tahoma", "weight": 400},
            "fontSize": {"magnitude": 11, "unit": "PT"}}


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> None:
    import argparse
    if not argv or argv[0] not in ("preview", "apply"):
        print(json.dumps({"error": "verb required: preview | apply"}, ensure_ascii=False))
        sys.exit(2)
    p = argparse.ArgumentParser(description="RECORDING DOCS builder")
    p.add_argument("--script-doc", required=True, help="Daily script Doc ID or URL")
    p.add_argument("--rundown-doc", default=None, help="RUNDOWN Doc ID (default: the standing doc)")
    p.add_argument("--prompter-doc", default=None, help="PROMPTER Doc ID (default: the standing doc)")
    args = p.parse_args(argv[1:])
    fn = apply_recording_docs if argv[0] == "apply" else preview_recording_docs
    print(json.dumps(fn(args.script_doc, args.rundown_doc, args.prompter_doc), ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
