"""RECORDING DOCS — build the RUNDOWN + PROMPTER recording docs from a daily script.

From the script doc's "NBTWB EVE RUNDOWN" + "NL RUNDOWN" tabs:
- RUNDOWN doc: both rundown lists — titles stripped of [tags], UPPERCASED,
  long titles wrapped ~7 words/line (split at the midpoint) — jingle marker
  tables, SB/CG lines. The Anchor block at the bottom is preserved untouched.
  The doc is renamed to "RUNDOWN NL-NWB DDMMYY" (date parsed from the script).
- PROMPTER doc: every story's intro cell (the first table under each numbered
  story H1, verbatim incl. bold/italic runs), plus the EVE outro, the NL
  show-open table (jingle + open + first story intro in one cell) and the NL
  ending — stacked as 1-row tables, EVE block + "++++" + NL block.

Writes only on apply; preview is read-only. Idempotent (replace-based).
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

# Titles longer than this many words split into two lines at the midpoint
# ("around 7 words per line, double lines for long title names").
WRAP_AT_WORDS = 8


# ---------------------------------------------------------------- helpers

def _text(e: dict) -> str:
    return "".join(el.get("textRun", {}).get("content", "")
                   for el in e.get("paragraph", {}).get("elements", []))


def _runs(e: dict) -> list[tuple[str, dict]]:
    out = []
    for el in e.get("paragraph", {}).get("elements", []):
        tr = el.get("textRun")
        if not tr:
            continue
        st = tr.get("textStyle", {}) or {}
        style = {k: True for k in ("bold", "italic") if st.get(k)}
        out.append((tr.get("content", ""), style))
    return out


def _para_style(e: dict) -> str:
    return e.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")


def _table_cell_paras(e: dict) -> list[dict]:
    try:
        return [p for p in e["table"]["tableRows"][0]["tableCells"][0]["content"] if "paragraph" in p]
    except Exception:
        return []


def _table_text(e: dict) -> str:
    return "\n".join(_text(p).rstrip("\n") for p in _table_cell_paras(e)).strip()


def _table_runs(e: dict) -> list[tuple[str, dict]]:
    """Styled runs of a 1x1 table's cell; paragraph boundaries kept as trailing
    newlines on the runs (dropping the cell's final break)."""
    paras = _table_cell_paras(e)
    out: list[tuple[str, dict]] = []
    for i, p in enumerate(paras):
        rs = _runs(p) or [("", {})]
        if i < len(paras) - 1 and not rs[-1][0].endswith("\n"):
            rs = rs[:-1] + [(rs[-1][0] + "\n", rs[-1][1])]
        out.extend(rs)
    if out and out[-1][0].endswith("\n"):
        out[-1] = (out[-1][0].rstrip("\n"), out[-1][1])
    return [(t, s) for t, s in out if t != ""]


_BREAK_STOPWORDS = {"IN", "OF", "THE", "A", "AN", "AND", "TO", "FOR", "ON",
                    "AT", "WITH", "FROM", "AS", "BY", "NOT"}


def _wrap_title(num: str, text: str) -> list[str]:
    """'2. [ต้นน้ำ] Cabinet Approval …' → ['2. CABINET APPROVAL SOUGHT TO BUILD',
    '504 KM OF SOUTHERN DOUBLE TRACK RAILWAY'] (tag-stripped, uppercased,
    midpoint-wrapped when longer than WRAP_AT_WORDS words; the break avoids
    landing right after a preposition/article)."""
    text = _TAG_RE.sub("", text).strip()
    words = text.split()
    if len(words) <= WRAP_AT_WORDS:
        return [f"{num}. {text.upper()}"]
    mid = (len(words) + 1) // 2
    while 1 < mid < len(words) - 1 and words[mid - 1].strip(",:;").upper() in _BREAK_STOPWORDS:
        mid -= 1
    return [f"{num}. {' '.join(words[:mid]).upper()}", " ".join(words[mid:]).upper()]


# ---------------------------------------------------------------- extraction

def _tab(doc: dict, needle: str) -> list[dict] | None:
    for t in doc.get("tabs", []):
        if needle.lower() in t.get("tabProperties", {}).get("title", "").lower():
            return t["documentTab"]["body"]["content"]
    return None


def _extract_rundown(els: list[dict]) -> dict:
    """The rundown LIST region of a tab → header + RUNDOWN-doc blocks."""
    header = ""
    blocks: list[dict] = []
    in_sb = False
    started = False
    for e in els:
        if "paragraph" in e:
            raw = _text(e)
            txt = raw.strip()
            if _para_style(e) == "TITLE":
                header = txt
                started = True
                continue
            if not started or not txt:
                continue
            m = _NUM_RE.match(raw)
            if m:
                for line in _wrap_title(m.group(1), m.group(2)):
                    blocks.append({"kind": "line", "text": line})
                in_sb = False
                continue
            if txt.startswith("ผู้ประกาศ") or re.match(r"^/+$", txt):
                blocks.append({"kind": "line", "text": txt})
                in_sb = False
                continue
            if re.match(r"^(SB|CG)\d*", txt):
                blocks.append({"kind": "line", "text": txt.upper()})
                in_sb = True
                continue
            if in_sb and raw[:1] == " " and not _THAI_RE.search(txt):
                blocks.append({"kind": "line", "text": txt.upper()})  # SB/CG continuation
                continue
            if _THAI_RE.search(txt):
                continue  # Thai summary lines under numbered titles
            blocks.append({"kind": "line", "text": txt.upper()})
        elif "table" in e and started:
            cell = _table_text(e)
            blocks.append({"kind": "table", "text": cell})
            if "END CREDIT" in cell:
                break  # NL: rundown list ends at (and includes) the end-credit table
            if "โยนเบรค" in cell:
                blocks.pop()  # EVE: the throw-to-break table is not part of the rundown
                break
    return {"header": header, "blocks": blocks}


def _extract_prompter(els: list[dict]) -> dict:
    """Story intro cells + show open / outro / ending for the PROMPTER doc."""
    stories: list[list[tuple[str, dict]]] = []
    open_runs = None
    outro_runs = None
    ending_runs = None
    pending_story = False
    first_story_seen = False
    in_signoff = False
    for e in els:
        if "paragraph" in e:
            txt = _text(e).strip()
            if _para_style(e) == "HEADING_1":
                if _NUM_RE.match(txt):
                    pending_story = True
                    first_story_seen = True
                    in_signoff = False
                elif txt.startswith("ผู้ประกาศพูดลา"):
                    in_signoff = True
        elif "table" in e:
            if pending_story:
                stories.append(_table_runs(e))
                pending_story = False
            elif in_signoff and outro_runs is None:
                outro_runs = _table_runs(e)
            elif not first_story_seen:
                open_runs = _table_runs(e)  # last pre-story table = show-open cell
            else:
                ending_runs = _table_runs(e)  # bare table after the last story
    return {"open": open_runs, "intros": stories, "outro": outro_runs, "ending": ending_runs}


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
    if not eve_p["intros"]:
        errors.append("EVE: no story intro cells found")
    if not nl_p["intros"]:
        errors.append("NL: no story intro cells found")
    if nl_p["open"] is None:
        errors.append("NL: show-open cell not found")
    if eve_p["outro"] is None:
        errors.append("EVE: outro cell not found")
    if nl_p["ending"] is None:
        errors.append("NL: ending cell not found")

    return {"script_doc": extract_doc_id(script_doc_id), "date": date_iso,
            "rundown_name": f"RUNDOWN NL-NWB {date_ddmmyy}" if date_ddmmyy else "",
            "eve": eve, "nl": nl, "prompter_eve": eve_p, "prompter_nl": nl_p,
            "errors": errors}


def _row_heads(runs_list: list[list[tuple[str, dict]]]) -> list[str]:
    return [("".join(t for t, _ in runs))[:80] or "(empty)" for runs in runs_list]


def preview_recording_docs(script_doc_id: str, rundown_doc_id: str | None = None,
                           prompter_doc_id: str | None = None) -> dict:
    data = extract_recording_docs(script_doc_id)
    if not data.get("eve"):
        return {"errors": data.get("errors", ["extraction failed"])}
    eve_p, nl_p = data["prompter_eve"], data["prompter_nl"]
    return {
        "script_doc": data["script_doc"],
        "date": data["date"],
        "rundown_name": data["rundown_name"],
        "rundown_doc": extract_doc_id(rundown_doc_id) if rundown_doc_id else DEFAULT_RUNDOWN_DOC,
        "prompter_doc": extract_doc_id(prompter_doc_id) if prompter_doc_id else DEFAULT_PROMPTER_DOC,
        "eve": {"header": data["eve"]["header"],
                "lines": [b["text"] for b in data["eve"]["blocks"] if b["kind"] == "line"],
                "tables": [b["text"][:40] for b in data["eve"]["blocks"] if b["kind"] == "table"]},
        "nl": {"header": data["nl"]["header"],
               "lines": [b["text"] for b in data["nl"]["blocks"] if b["kind"] == "line"],
               "tables": [b["text"][:40] for b in data["nl"]["blocks"] if b["kind"] == "table"]},
        "prompter": {
            "eve_rows": _row_heads(eve_p["intros"] + ([eve_p["outro"]] if eve_p["outro"] else [])),
            "nl_rows": _row_heads(
                ([nl_p["open"]] if nl_p["open"] else [])
                + (nl_p["intros"][1:] if nl_p["open"] else nl_p["intros"])
                + ([nl_p["ending"]] if nl_p["ending"] else [])),
        },
        "errors": data["errors"],
    }


# ---------------------------------------------------------------- apply

def _batch(doc_id: str, requests: list[dict]) -> None:
    _api("POST", f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
         body={"requests": requests})


def _insert_text_at(doc_id: str, index: int, text: str) -> None:
    if text:
        _batch(doc_id, [{"insertText": {"location": {"index": index}, "text": text}}])


def _write_blocks(doc_id: str, seq: list[dict], delete_to: int | None = None) -> int:
    """Replace a doc's body (or everything before ``delete_to``) with ``seq``
    (paragraph / table / runs blocks). Structural inserts go in reverse at
    index 1; cells are then filled forward with a cumulative delta so indexes
    stay valid. Returns the number of tables written."""
    doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}")
    if delete_to is not None:
        end = delete_to - 1
    else:
        end = doc["body"]["content"][-1]["endIndex"] - 1
    if end > 1:
        _batch(doc_id, [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end}}}])
    for b in reversed(seq):
        if b["kind"] in ("table", "runs"):
            _batch(doc_id, [{"insertTable": {"rows": 1, "columns": 1, "location": {"index": 1}}}])
        else:
            _insert_text_at(doc_id, 1, f"{b.get('text', '')}\n")

    doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}")
    tables = [e for e in doc["body"]["content"] if "table" in e]
    cell_blocks = [b for b in seq if b["kind"] in ("table", "runs")]
    style_requests: list[dict] = []
    delta = 0  # text inserted into earlier tables shifts later cell indexes
    written = 0
    for t_el, b in zip(tables, cell_blocks):
        start = t_el["startIndex"] + delta
        try:
            cell_start = t_el["table"]["tableRows"][0]["tableCells"][0]["content"][0]["startIndex"]
            start = cell_start + delta
        except Exception:
            pass
        if b["kind"] == "table":
            _insert_text_at(doc_id, start, b["text"])
            delta += len(b["text"])
            written += 1
            continue
        pos = start
        for text, style in b["runs"]:
            _insert_text_at(doc_id, pos, text)
            if style:
                style_requests.append({"updateTextStyle": {
                    "range": {"startIndex": pos, "endIndex": pos + len(text)},
                    "textStyle": style, "fields": ",".join(sorted(style))}})
            pos += len(text)
        delta += sum(len(t) for t, _ in b["runs"])
        written += 1

    # TITLE style for the header paragraphs (matched by exact text)
    if any(b.get("style") == "TITLE" for b in seq):
        doc2 = _api("GET", f"https://docs.googleapis.com/v1/documents/{doc_id}")
        headers = {b["text"].strip() for b in seq if b.get("style") == "TITLE"}
        for e in doc2["body"]["content"]:
            if "paragraph" in e and _text(e).strip() in headers:
                style_requests.append({"updateParagraphStyle": {
                    "range": {"startIndex": e["startIndex"], "endIndex": e["endIndex"] - 1},
                    "paragraphStyle": {"namedStyleType": "TITLE"}, "fields": "namedStyleType"}})
    if style_requests:
        _batch(doc_id, style_requests)
    return written


def apply_recording_docs(script_doc_id: str, rundown_doc_id: str | None = None,
                         prompter_doc_id: str | None = None) -> dict:
    data = extract_recording_docs(script_doc_id)
    fatal = [e for e in data.get("errors", []) if "no " in e]
    if fatal:
        return {"applied": False, "errors": fatal}
    rundown_id = extract_doc_id(rundown_doc_id) if rundown_doc_id else DEFAULT_RUNDOWN_DOC
    prompter_id = extract_doc_id(prompter_doc_id) if prompter_doc_id else DEFAULT_PROMPTER_DOC
    out = {"applied": True, "rundown_doc": rundown_id, "prompter_doc": prompter_id,
           "rundown_name": data["rundown_name"], "errors": data.get("errors", [])}

    # ── RUNDOWN doc: rename, then replace everything above the Anchor block ──
    if data["rundown_name"]:
        cur = _api("GET", f"https://www.googleapis.com/drive/v3/files/{rundown_id}",
                   params={"fields": "name"})
        if cur.get("name") != data["rundown_name"]:
            _api("PATCH", f"https://www.googleapis.com/drive/v3/files/{rundown_id}",
                 body={"name": data["rundown_name"]})
            out["renamed_to"] = data["rundown_name"]
    doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{rundown_id}")
    anchor = None
    for e in doc["body"]["content"]:
        if "paragraph" in e and _text(e).strip() == "Anchor" and _para_style(e).startswith("HEADING"):
            anchor = e["startIndex"]
            break
    if anchor is None:
        return {"applied": False, "errors": ["RUNDOWN doc has no 'Anchor' heading to preserve"]}

    seq: list[dict] = [{"kind": "para", "text": data["eve"]["header"], "style": "TITLE"}]
    seq += data["eve"]["blocks"]
    seq += [{"kind": "para", "text": data["nl"]["header"], "style": "TITLE"}]
    seq += data["nl"]["blocks"]
    seq += [{"kind": "para", "text": " "}]  # spacer before the Anchor block
    # delete_to=anchor → everything above the Anchor block is replaced;
    # the Anchor block itself is never touched (styling preserved).
    out["rundown_tables"] = _write_blocks(rundown_id, seq, delete_to=anchor)

    # ── PROMPTER doc: full replace ──
    pseq: list[dict] = [{"kind": "runs", "runs": r} for r in data["prompter_eve"]["intros"]]
    if data["prompter_eve"]["outro"]:
        pseq.append({"kind": "runs", "runs": data["prompter_eve"]["outro"]})
    pseq.append({"kind": "para", "text": "++++"})
    if data["prompter_nl"]["open"]:
        # the open cell already carries story 1's intro (combined by the writer)
        pseq.append({"kind": "runs", "runs": data["prompter_nl"]["open"]})
        nl_intros = data["prompter_nl"]["intros"][1:]
    else:
        nl_intros = data["prompter_nl"]["intros"]
    pseq += [{"kind": "runs", "runs": r} for r in nl_intros]
    if data["prompter_nl"]["ending"]:
        pseq.append({"kind": "runs", "runs": data["prompter_nl"]["ending"]})
    out["prompter_tables"] = _write_blocks(prompter_id, pseq)
    return out


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
