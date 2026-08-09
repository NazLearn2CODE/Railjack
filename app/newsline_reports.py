#!/usr/bin/env python3
"""NEWSLINE Reports — monthly NBT contractor work-report docs generator.

Generates Naz's monthly NBT contractor work-report docs from a period no. and date range:
  - (A) Cover doc: Duplicated from template 1vqhrBRUUgbDSX9PoNBaRWDr6mqPSSgU7,
        dates rendered in western numerals (D <Thai-month> <BE-year>), QR code preserved.
  - (C) Log doc: Duplicated from template 1FFRqsOV8XdgDPAlM0u0Vyzak8LyN71bc,
        header filled with period & fiscal year, body with one row per weekday Mon-Fri
        in the range formatted with Thai numerals (e.g. ๑ ตุลาคม ๒๕๖๘  รายการ NEWSLINE).

Both docs are saved into the fiscal-year folder inside destination folder
1aregEEnnZPm2JhP2_-S0X03f8a-5ViuN. Idempotent per (period, FY).

House style (mirrors radio.py + newsline.py + nl_append.py): stdlib only
(urllib/json/argparse/calendar/datetime/zipfile/xml/io), one compact JSON object
to stdout, and {"_fatal": ...} + exit 1 on handled failure — app/newsroom.py turns
_fatal into a clean HTTP 400.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from _oauth_err import token_error
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from _oauth_err import token_error
    except ImportError:
        def token_error(e: Exception | dict) -> str:
            return f"OAuth token error: {e}"

# --- binding contract (NEWSLINE-REPORTS-BRIEF.md) ------------------------
DEST_ROOT_FOLDER = "1aregEEnnZPm2JhP2_-S0X03f8a-5ViuN"  # "ส่งงาน NEWLINE (Producer)"
TEMPLATE_COVER = "1vqhrBRUUgbDSX9PoNBaRWDr6mqPSSgU7"
TEMPLATE_LOG = "1FFRqsOV8XdgDPAlM0u0Vyzak8LyN71bc"
TEMPLATE_RUNDOWN = "15T7kZZqmlGKHkvhysbzVA79PH_PcEB1weu10zR1ygSM"

DEFAULT_TOKEN = Path.home() / ".config" / "railjack" / "google_token.json"
TOKEN_PATH = Path(os.environ.get("NEWSLINE_REPORTS_TOKEN_PATH",
                                os.environ.get("NEWSLINE_TOKEN_PATH", str(DEFAULT_TOKEN))))
DRIVE = "https://www.googleapis.com/drive/v3/files"
UPLOAD_DRIVE = "https://www.googleapis.com/upload/drive/v3/files"

THAI_MONTHS = [
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
]
ENGLISH_MONTHS = [
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"
]
MONTH_MAP_EN = {m: i + 1 for i, m in enumerate(ENGLISH_MONTHS)}
MONTH_MAP_TH = {m: i + 1 for i, m in enumerate(THAI_MONTHS)}
THAI_DIGITS = str.maketrans("0123456789", "๐๑๒๓๔๕๖๗๘๙")

XML_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}
ET.register_namespace("w", "http://schemas.openxmlformats.org/wordprocessingml/2006/main")
ET.register_namespace("w14", "http://schemas.microsoft.com/office/word/2010/wordml")

_TOKEN: str | None = None  # module-level cache: refresh the token once per run


def _fatal(msg: str):
    """Handled failure: print the contract payload and exit 1."""
    print(json.dumps({"_fatal": msg}))
    sys.exit(1)


def google_token() -> str:
    """Load + refresh the railjack Google OAuth token (cached per run).
    Never prints or logs the token."""
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    if not TOKEN_PATH.exists():
        _fatal("railjack Google token not found — run `python3 -m app.tn_auth` once")
    d = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({
        "client_id": d["client_id"], "client_secret": d["client_secret"],
        "refresh_token": d["refresh_token"], "grant_type": "refresh_token",
    })
    req = urllib.request.Request(
        d["token_uri"], data=body.encode(), method="POST",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
    except Exception as e:
        _fatal(token_error(e))
    if "access_token" not in resp:
        _fatal(token_error(resp))
    _TOKEN = resp["access_token"]
    return _TOKEN  # type: ignore[return-value]


def _api(method: str, url: str, body=None, params=None, headers=None, raw_response: bool = False):
    """Drive REST helper (stdlib urllib): JSON in, JSON/bytes out."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None
    h = {
        "Authorization": f"Bearer {google_token()}",
    }
    if headers:
        h.update(headers)
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
            if "Content-Type" not in h:
                h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        _fatal(f"Drive {method} failed: {e.code} {e.reason} {detail}".strip())
    except Exception as e:
        _fatal(f"Drive {method} failed: {e}")
    if raw_response:
        return raw
    return json.loads(raw.decode("utf-8")) if raw else {}


# --- Thai date formatting helpers ----------------------------------------


def to_thai_digits(s: int | str) -> str:
    """Convert western digits 0-9 to Thai numerals ๐-๙."""
    return str(s).translate(THAI_DIGITS)


def be_year(ce_year: int) -> int:
    """CE year to Buddhist Era (BE) year: CE + 543."""
    return ce_year + 543


def fy_be(ce_year: int, month: int) -> int:
    """Derive Fiscal Year (BE) from CE year and month (Thai FY starts Oct 1).
    FY_BE = CE_year + 543 + (1 if month >= 10 else 0)"""
    return ce_year + 543 + (1 if month >= 10 else 0)


def format_thai_western(d: datetime.date) -> str:
    """Western numerals date in Thai month: 'D <Thai-month> <BE-year>'
    e.g. '21 กุมภาพันธ์ 2569'"""
    return f"{d.day} {THAI_MONTHS[d.month - 1]} {be_year(d.year)}"


def format_thai_numerals(d: datetime.date) -> str:
    """Thai numerals date in Thai month: 'D <Thai-month> <BE-year>'
    e.g. '๒๑ กุมภาพันธ์ ๒๕๖๙'"""
    return f"{to_thai_digits(d.day)} {THAI_MONTHS[d.month - 1]} {to_thai_digits(be_year(d.year))}"


def weekdays_in_range(start_d: datetime.date, end_d: datetime.date) -> list[datetime.date]:
    """All Mon-Fri weekdays in [start_d, end_d] inclusive. Include public holidays."""
    cur = start_d
    res = []
    while cur <= end_d:
        if cur.weekday() < 5:  # 0=Mon, 4=Fri
            res.append(cur)
        cur += datetime.timedelta(days=1)
    return res


def weekday_rows(weekdays: list[datetime.date]) -> list[str]:
    """Enumerated Mon-Fri weekday row texts with Thai numerals:
    '<D> <Thai-month> <BE-year>  รายการ NEWSLINE'"""
    return [f"{format_thai_numerals(d)}  รายการ NEWSLINE" for d in weekdays]


def cover_doc_name(period: int | str, start_d: datetime.date, end_d: datetime.date) -> str:
    """Generate target filename for Cover doc (preserves .docx):
    '<period> ใบรายงานผลการปฏิบัติงาน แบบ QR Code <Thai-month> <BE-year> ณอรรฆย์ โรจนสุวรรณ.docx'"""
    p = str(period).strip()
    month_name = THAI_MONTHS[end_d.month - 1]
    year = be_year(end_d.year)
    return f"{p} ใบรายงานผลการปฏิบัติงาน แบบ QR Code {month_name} {year} ณอรรฆย์ โรจนสุวรรณ.docx"


def log_doc_name(period: int | str, start_d: datetime.date, end_d: datetime.date) -> str:
    """Generate target filename for Log doc (preserves .docx):
    '<period> รายงานผลการปฏิบัติงาน <Thai-month> <BE-year>.docx'"""
    p = str(period).strip()
    month_name = THAI_MONTHS[end_d.month - 1]
    year = be_year(end_d.year)
    return f"{p} รายงานผลการปฏิบัติงาน {month_name} {year}.docx"


# --- Pure plan builder ---------------------------------------------------


def parse_date(date_val: str | datetime.date) -> datetime.date:
    """Parse ISO date string 'YYYY-MM-DD' or return date object."""
    if isinstance(date_val, datetime.date):
        return date_val
    try:
        return datetime.date.fromisoformat(str(date_val).strip())
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(str(date_val).strip(), fmt).date()
        except ValueError:
            pass
    _fatal(f"invalid date format: '{date_val}' — use YYYY-MM-DD")
    raise ValueError(date_val)


def build_plan(period: int | str, start_d: datetime.date, end_d: datetime.date) -> dict:
    """Pure compute (no network): computes FY, filenames, and Mon-Fri weekday rows."""
    if start_d > end_d:
        _fatal(f"start date ({start_d}) cannot be after end date ({end_d})")
    fy = fy_be(start_d.year, start_d.month)
    weekdays = weekdays_in_range(start_d, end_d)
    rows = weekday_rows(weekdays)
    cover_name = cover_doc_name(period, start_d, end_d)
    log_name = log_doc_name(period, start_d, end_d)
    return {
        "period": str(period).strip(),
        "fy_be": fy,
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "start_display": format_thai_western(start_d),
        "end_display": format_thai_western(end_d),
        "start_display_th": format_thai_numerals(start_d),
        "end_display_th": format_thai_numerals(end_d),
        "cover_filename": cover_name,
        "log_filename": log_name,
        "weekday_count": len(weekdays),
        "rows": rows,
    }


# --- Docx XML transformation helpers -------------------------------------


def fill_cover_xml(xml_bytes: bytes, period: int | str, start_d: datetime.date, end_d: datetime.date) -> bytes:
    """Modify word/document.xml for Cover doc:
    Updates paragraph 0 with western numeral period and date range."""
    tree = ET.fromstring(xml_bytes)
    start_str = format_thai_western(start_d)
    end_str = format_thai_western(end_d)
    p_str = str(period).strip()
    new_p0_text = f"รายงานผลการปฏิบัติงานประจำ งวดที่....{p_str}......ระหว่างวันที่....{start_str} – {end_str}....."

    body = tree.find("w:body", XML_NS)
    if body is not None:
        for p in body.findall("w:p", XML_NS):
            p_text = "".join([t.text for t in p.iterfind(".//w:t", XML_NS) if t.text])
            if "รายงานผลการปฏิบัติงานประจำ" in p_text:
                runs = p.findall("w:r", XML_NS)
                if runs:
                    first_t = runs[0].find("w:t", XML_NS)
                    if first_t is not None:
                        first_t.text = new_p0_text
                    for r in runs[1:]:
                        p.remove(r)
                break
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True)


def fill_log_xml(xml_bytes: bytes, period: int | str, start_d: datetime.date,
                 end_d: datetime.date, weekdays: list[datetime.date]) -> bytes:
    """Modify word/document.xml for Log doc:
    Updates header with period & fiscal year (Thai numerals), date range, and replaces
    body weekday sample rows with full Mon-Fri weekdays list."""
    tree = ET.fromstring(xml_bytes)
    fy = fy_be(start_d.year, start_d.month)
    period_th = to_thai_digits(period)
    fy_th = to_thai_digits(fy)
    start_th = format_thai_numerals(start_d)
    end_th = format_thai_numerals(end_d)

    body = tree.find("w:body", XML_NS)
    if body is None:
        return xml_bytes

    sectPr = body.find("w:sectPr", XML_NS)

    # 1. Update Header paragraphs
    for p in body.findall("w:p", XML_NS):
        p_text = "".join([t.text for t in p.iterfind(".//w:t", XML_NS) if t.text])
        if "รายงานผลการปฏิบัติงานงวดที่" in p_text:
            pPr = p.find("w:pPr", XML_NS)
            if pPr is not None:
                rPr = pPr.find("w:rPr", XML_NS)
                if rPr is not None:
                    col = rPr.find("w:color", XML_NS)
                    if col is not None:
                        rPr.remove(col)
            runs = p.findall("w:r", XML_NS)
            if runs:
                t = runs[0].find("w:t", XML_NS)
                if t is not None:
                    t.text = f"รายงานผลการปฏิบัติงานงวดที่ {period_th} ปีงบประมาณ {fy_th}"
                for r in runs[1:]:
                    p.remove(r)
        elif "ตั้งแต่วันที่" in p_text:
            pPr = p.find("w:pPr", XML_NS)
            if pPr is not None:
                rPr = pPr.find("w:rPr", XML_NS)
                if rPr is not None:
                    col = rPr.find("w:color", XML_NS)
                    if col is not None:
                        rPr.remove(col)
            runs = p.findall("w:r", XML_NS)
            if runs:
                t = runs[0].find("w:t", XML_NS)
                if t is not None:
                    t.text = f"ตั้งแต่วันที่ {start_th} - {end_th}"
                for r in runs[1:]:
                    p.remove(r)

    # 2. Extract sample weekday heading and newsline paragraph prototypes
    all_p = body.findall("w:p", XML_NS)
    sample_date_p = None
    sample_newsline_p = None
    for p in all_p:
        t = "".join([elem.text for elem in p.iterfind(".//w:t", XML_NS) if elem.text])
        if "วันที่ ๑ ตุลาคม" in t and sample_date_p is None:
            sample_date_p = copy.deepcopy(p)
        elif "รายการ NEWSLINE" in t and sample_newsline_p is None:
            sample_newsline_p = copy.deepcopy(p)

    # Fallback paragraph prototypes if not found
    if sample_date_p is None:
        sample_date_p = ET.Element("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
        pPr = ET.SubElement(sample_date_p, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
        rPr = ET.SubElement(pPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr")
        ET.SubElement(rPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts",
                      {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ascii": "Sarabun",
                       "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}cs": "Sarabun"})
        ET.SubElement(rPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}b",
                      {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val": "1"})
        ET.SubElement(rPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sz",
                      {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val": "28"})
        ET.SubElement(rPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}u",
                      {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val": "single"})
        r = ET.SubElement(sample_date_p, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r")
        ET.SubElement(r, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
                      {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})

    if sample_newsline_p is None:
        sample_newsline_p = ET.Element("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
        pPr = ET.SubElement(sample_newsline_p, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr")
        rPr = ET.SubElement(pPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr")
        ET.SubElement(rPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts",
                      {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ascii": "Sarabun",
                       "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}cs": "Sarabun"})
        ET.SubElement(rPr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sz",
                      {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val": "24"})
        r = ET.SubElement(sample_newsline_p, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r")
        t_el = ET.SubElement(r, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
                             {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
        t_el.text = "รายการ NEWSLINE"

    # 3. Strip old sample body paragraphs after header
    keep = True
    for p in list(body.findall("w:p", XML_NS)):
        t = "".join([elem.text for elem in p.iterfind(".//w:t", XML_NS) if elem.text])
        if "ตั้งแต่วันที่" in t:
            keep = False
            continue
        if not keep:
            body.remove(p)

    # 4. Insert header spacer paragraph
    spacer = ET.Element("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
    body.append(spacer)

    # 5. Append full list of Mon-Fri weekday paragraphs
    for d in weekdays:
        # Date header
        date_p = copy.deepcopy(sample_date_p)
        t_node = date_p.find(".//w:t", XML_NS)
        if t_node is None:
            r = date_p.find("w:r", XML_NS)
            if r is None:
                r = ET.SubElement(date_p, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r")
            t_node = ET.SubElement(r, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        t_node.text = f"วันที่ {format_thai_numerals(d)}"
        body.append(date_p)

        # Newsline row
        nl_p = copy.deepcopy(sample_newsline_p)
        t_node_nl = nl_p.find(".//w:t", XML_NS)
        if t_node_nl is None:
            r = nl_p.find("w:r", XML_NS)
            if r is None:
                r = ET.SubElement(nl_p, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r")
            t_node_nl = ET.SubElement(r, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        t_node_nl.text = "รายการ NEWSLINE"
        body.append(nl_p)

        # Day spacer
        body.append(ET.Element("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"))

    # 6. Ensure sectPr is preserved at the end of body
    if sectPr is not None:
        body.remove(sectPr)
        body.append(sectPr)

    return ET.tostring(tree, encoding="utf-8", xml_declaration=True)


def fill_cover_docx(data: bytes, period: int | str, start_d: datetime.date, end_d: datetime.date) -> bytes:
    """Update word/document.xml in Cover docx zip archive, preserving QR image and all other media."""
    in_buf = io.BytesIO(data)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(in_buf, "r") as zin, zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            content = zin.read(item.filename)
            if item.filename == "word/document.xml":
                content = fill_cover_xml(content, period, start_d, end_d)
            zout.writestr(item, content)
    return out_buf.getvalue()


def fill_log_docx(data: bytes, period: int | str, start_d: datetime.date,
                  end_d: datetime.date, weekdays: list[datetime.date]) -> bytes:
    """Update word/document.xml in Log docx zip archive, preserving all formatting and assets."""
    in_buf = io.BytesIO(data)
    out_buf = io.BytesIO()
    with zipfile.ZipFile(in_buf, "r") as zin, zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            content = zin.read(item.filename)
            if item.filename == "word/document.xml":
                content = fill_log_xml(content, period, start_d, end_d, weekdays)
            zout.writestr(item, content)
    return out_buf.getvalue()


# --- Drive API operations ------------------------------------------------


def list_subfolders(parent_id: str) -> list[dict]:
    """List child folders inside parent_id."""
    q = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    data = _api("GET", DRIVE, params={
        "q": q, "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true", "fields": "files(id,name)",
    })
    return data.get("files", [])


def create_folder(name: str, parent_id: str) -> str:
    """Create a folder inside parent_id."""
    data = _api("POST", DRIVE, body={
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }, params={"supportsAllDrives": "true", "fields": "id,name"})
    return data.get("id", "")


def find_or_create_fy_folder(root_id: str, fy: int, dry: bool = False) -> tuple[str | None, str, bool]:
    """Find folder 'งบประมาณ {fy}' inside root_id, or create it if missing."""
    target_name = f"งบประมาณ {fy}"
    for f in list_subfolders(root_id):
        if f["name"] == target_name:
            return f["id"], f["name"], False
    if dry:
        return None, target_name, True
    fid = create_folder(target_name, root_id)
    return fid, target_name, True


def find_existing_files(folder_id: str) -> list[dict]:
    """List all non-trashed files in folder_id."""
    files: list[dict] = []
    page = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,webViewLink)",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        data = _api("GET", DRIVE, params=params)
        files.extend(data.get("files", []))
        page = data.get("nextPageToken")
        if not page:
            break
    return files


def copy_file(template_id: str, name: str, parent_id: str) -> tuple[str, str, str]:
    """Duplicate template into parent folder via Drive files.copy."""
    data = _api("POST", f"{DRIVE}/{template_id}/copy", body={
        "name": name, "parents": [parent_id],
    }, params={"supportsAllDrives": "true", "fields": "id,name,webViewLink"})
    return data.get("id", ""), data.get("name", name), data.get("webViewLink", "")


def download_media(file_id: str) -> bytes:
    """Download binary content of a file from Google Drive."""
    return _api("GET", f"{DRIVE}/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}, raw_response=True)


def upload_media(file_id: str, media_bytes: bytes) -> dict:
    """Update binary media of an existing Drive file."""
    return _api(
        "PATCH",
        f"{UPLOAD_DRIVE}/{file_id}",
        params={"uploadType": "media", "supportsAllDrives": "true"},
        headers={"Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        body=media_bytes,
    )


# --- Execution engine ----------------------------------------------------


def generate_reports(period: int | str, start_d: datetime.date,
                     end_d: datetime.date, dry_run: bool = False) -> dict:
    """Preview or generate NEWSLINE contractor work-reports."""
    plan = build_plan(period, start_d, end_d)
    fy = plan["fy_be"]

    # 1. Resolve target FY folder (e.g. 'งบประมาณ 2569')
    folder_id, folder_name, folder_created = find_or_create_fy_folder(DEST_ROOT_FOLDER, fy, dry=dry_run)

    if dry_run:
        return {
            "dry_run": True,
            **plan,
            "folder": {"id": folder_id, "name": folder_name, "created": folder_created},
            "cover": {"name": plan["cover_filename"], "template_id": TEMPLATE_COVER},
            "log": {"name": plan["log_filename"], "template_id": TEMPLATE_LOG},
            "created": [],
            "skipped": [],
        }

    # 2. Check for existing documents (Idempotency)
    existing = find_existing_files(folder_id)
    existing_cover = None
    existing_log = None
    c_base = plan["cover_filename"].replace(".docx", "")
    l_base = plan["log_filename"].replace(".docx", "")
    month_name = THAI_MONTHS[end_d.month - 1]

    for f in existing:
        fname = f.get("name", "")
        if fname.startswith("###"):
            continue
        if c_base in fname or ("ใบรายงานผลการปฏิบัติงาน" in fname and month_name in fname and (str(fy) in fname or to_thai_digits(fy) in fname)):
            existing_cover = f
        elif l_base in fname or ("รายงานผลการปฏิบัติงาน" in fname and "ใบรายงาน" not in fname and month_name in fname and (str(fy) in fname or to_thai_digits(fy) in fname)):
            existing_log = f

    # If both already exist in the target folder, return existing without duplicating
    if existing_cover and existing_log:
        cov_res = {"id": existing_cover["id"], "name": existing_cover["name"], "url": existing_cover.get("webViewLink", "")}
        log_res = {"id": existing_log["id"], "name": existing_log["name"], "url": existing_log.get("webViewLink", "")}
        return {
            "dry_run": False,
            "idempotent": True,
            **plan,
            "folder": {"id": folder_id, "name": folder_name},
            "cover": cov_res,
            "log": log_res,
            "created": [],
            "skipped": [existing_cover["name"], existing_log["name"]],
        }

    # 3. Duplicate templates & fill contents
    weekdays = weekdays_in_range(start_d, end_d)

    # Cover doc
    if existing_cover:
        cov_res = {"id": existing_cover["id"], "name": existing_cover["name"], "url": existing_cover.get("webViewLink", "")}
    else:
        cid, cname, clink = copy_file(TEMPLATE_COVER, plan["cover_filename"], folder_id)
        raw_cov = download_media(cid)
        filled_cov = fill_cover_docx(raw_cov, period, start_d, end_d)
        upload_media(cid, filled_cov)
        cov_res = {"id": cid, "name": cname, "url": clink}

    # Log doc
    if existing_log:
        log_res = {"id": existing_log["id"], "name": existing_log["name"], "url": existing_log.get("webViewLink", "")}
    else:
        lid, lname, llink = copy_file(TEMPLATE_LOG, plan["log_filename"], folder_id)
        raw_log = download_media(lid)
        filled_log = fill_log_docx(raw_log, period, start_d, end_d, weekdays)
        upload_media(lid, filled_log)
        log_res = {"id": lid, "name": lname, "url": llink}

    return {
        "dry_run": False,
        "idempotent": False,
        **plan,
        "folder": {"id": folder_id, "name": folder_name},
        "cover": cov_res,
        "log": log_res,
        "created": [cov_res, log_res],
        "skipped": [],
    }


# --- Sub-tab ①: NEWSLINE Rundown Extraction & Monthly Doc Fill ----------


def extract_doc_id(id_or_url: str) -> str:
    """Extract 25+ char Google Doc / Drive file ID from a raw ID or URL string."""
    s = (id_or_url or "").strip()
    if not s:
        return ""
    m = re.search(r'/d/([a-zA-Z0-9_-]{25,})', s)
    if m:
        return m.group(1)
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]{25,})', s)
    if m:
        return m.group(1)
    m = re.search(r'^[a-zA-Z0-9_-]{25,}$', s)
    if m:
        return m.group(0)
    m = re.search(r'[a-zA-Z0-9_-]{25,}', s)
    if m:
        return m.group(0)
    return s


def find_anchor_in_doc(doc_data: dict, nl_paragraphs: list[tuple[dict, str]] | None = None) -> str | None:
    """Check daily doc (all tabs incl. NBTWB 'ผู้ประกาศ:' / 'ANCHOR:') for announcer/anchor name.
    Returns cleaned anchor name if reliably found, else None.
    """
    # 1. Check NL tab paragraphs
    if nl_paragraphs:
        for _, text in nl_paragraphs:
            line = text.strip()
            m = re.search(r'NEWSLINE\s+.*--\s*ANCHOR:\s*(.+)', line, re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                if cand and not re.match(r'^[_\-\.\s:]+$', cand):
                    return cand
            m_gen = re.search(r'(?:ANCHOR|ผู้ประกาศ|Announcer|Presenter)\s*[:：]\s*(.+)', line, re.IGNORECASE)
            if m_gen:
                cand = m_gen.group(1).strip()
                if cand and not re.match(r'^[_\-\.\s:]+$', cand):
                    return cand

    # 2. Check all tabs across the doc
    tabs = doc_data.get("tabs", [])
    for t in tabs:
        content = t.get("documentTab", {}).get("body", {}).get("content", [])
        if not content:
            content = t.get("body", {}).get("content", [])
        for el in content:
            if "paragraph" in el:
                p_text = "".join(e.get("textRun", {}).get("content", "") for e in el["paragraph"].get("elements", [])).strip()
                m = re.search(r'(?:ผู้ประกาศ|ANCHOR|Announcer|Presenter)\s*[:：]\s*([^\n\r]+)', p_text, re.IGNORECASE)
                if m:
                    cand = m.group(1).strip()
                    if cand and not re.match(r'^[_\-\.\s:]+$', cand) and len(cand) > 1:
                        return cand
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        for cp in cell.get("content", []):
                            if "paragraph" in cp:
                                p_text = "".join(e.get("textRun", {}).get("content", "") for e in cp["paragraph"].get("elements", [])).strip()
                                m = re.search(r'(?:ผู้ประกาศ|ANCHOR|Announcer|Presenter)\s*[:：]\s*([^\n\r]+)', p_text, re.IGNORECASE)
                                if m:
                                    cand = m.group(1).strip()
                                    if cand and not re.match(r'^[_\-\.\s:]+$', cand) and len(cand) > 1:
                                        return cand
    return None


def extract_nl_rundown_from_doc(doc_id: str, doc_data: dict | None = None) -> dict:
    """Extract NEWSLINE rundown (date header + numbered headlines) from daily Google Doc.

    Source: Daily 'NL & NWB DDMMYY' doc. Reads Tab index 3 (titled 'NL RUNDOWN', tabId 't.0').
    Extracts 1st page content before pageBreak or ***END CREDIT*** or script start.
    Preserves textRun styling (bold, font, size, foreground, etc.).
    """
    clean_id = extract_doc_id(doc_id)
    if not clean_id:
        _fatal("daily doc_id is required")

    if doc_data is None:
        doc_data = _api("GET", f"https://docs.googleapis.com/v1/documents/{clean_id}", params={"includeTabsContent": "true"})

    title = doc_data.get("title", "")
    tabs = doc_data.get("tabs", [])

    nl_tab = None
    for t in tabs:
        t_title = t.get("tabProperties", {}).get("title", "")
        if "NL RUNDOWN" in t_title.upper() or "NL" in t_title.upper():
            nl_tab = t
            break
    if not nl_tab and tabs:
        nl_tab = tabs[-1]

    if nl_tab:
        content = nl_tab.get("documentTab", {}).get("body", {}).get("content", [])
        if not content:
            content = nl_tab.get("body", {}).get("content", [])
    else:
        content = doc_data.get("body", {}).get("content", [])

    page1_paragraphs: list[tuple[dict, str]] = []
    for el in content:
        if "paragraph" in el:
            p = el["paragraph"]
            has_pb = any("pageBreak" in e for e in p.get("elements", []))
            text = "".join(e.get("textRun", {}).get("content", "") for e in p.get("elements", []))
            if has_pb:
                break
            if "SWDK." in text or "SWDK" in text or "Thank you for joining us" in text:
                break
            page1_paragraphs.append((p, text))
        elif "table" in el:
            t_text = ""
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    for cp in cell.get("content", []):
                        if "paragraph" in cp:
                            t_text += "".join(e.get("textRun", {}).get("content", "") for e in cp["paragraph"].get("elements", []))
            if "END CREDIT" in t_text:
                break

    header_line = None
    date_obj = None
    date_str = None
    anchor_name = find_anchor_in_doc(doc_data, page1_paragraphs)
    story_blocks: list[dict] = []
    current_story: dict | None = None

    for p, text in page1_paragraphs:
        line = text.strip()
        if not line:
            continue

        # Check header e.g. "NEWSLINE 05.AUGUST.2026 -- ANCHOR: Naz", "NEWSLINE 05.AUGUST.2026 -- ANCHOR:", or "NEWSLINE 05.AUGUST.2026"
        m_head = re.search(r'NEWSLINE\s+([0-9]{1,2})\.([A-Za-zก-๙]+)\.([0-9]{4})(?:\s*--\s*ANCHOR:?\s*(.*))?', line, re.IGNORECASE)
        if m_head:
            day_s, mon_s, yr_s = m_head.group(1), m_head.group(2).upper(), m_head.group(3)
            if not anchor_name and m_head.group(4):
                cand = m_head.group(4).strip()
                if cand and not re.match(r'^[_\-\.\s:]+$', cand):
                    anchor_name = cand
            day = int(day_s)
            mon = MONTH_MAP_EN.get(mon_s, MONTH_MAP_TH.get(mon_s, 1))
            yr = int(yr_s)
            ce_yr = yr - 543 if yr > 2400 else yr
            try:
                date_obj = datetime.date(ce_yr, mon, day)
            except ValueError:
                pass
            date_str = f"{day:02d}.{ENGLISH_MONTHS[mon - 1]}.{yr}"
            continue

        m_num = re.match(r'^(\d+)\.\s*(.*)', line)
        if m_num:
            num = int(m_num.group(1))
            rest = m_num.group(2).strip()
            current_story = {
                "num": num,
                "en_para": p,
                "en_lines": [rest],
                "th_lines": [],
                "th_paras": [],
            }
            story_blocks.append(current_story)
            continue

        if current_story:
            # Soundbite lines check
            if (re.match(r'^\s*SB\d*:', line, re.IGNORECASE) or
                line.startswith("Country General Manager") or
                line.startswith("Minister of") or
                line.startswith("President of") or
                line.startswith("General Conference") or
                line.startswith("TECO Representative") or
                line.startswith("Director, ISOC") or
                line.startswith("Thai Language Teacher") or
                line.startswith("Exchange Student")):
                continue
            if re.search(r'[\u0e00-\u0e7f]', line):
                current_story["th_lines"].append(line)
                current_story["th_paras"].append(p)
            else:
                current_story["en_lines"].append(line)

    # Fallback for date if not extracted from header
    if not date_obj:
        m_title = re.search(r'(\d{2})(\d{2})(\d{2})', title)
        if m_title:
            d_i, m_i, y_i = int(m_title.group(1)), int(m_title.group(2)), int(m_title.group(3))
            ce_yr = 2000 + y_i
            be_yr = ce_yr + 543
            mon = max(1, min(12, m_i))
            try:
                date_obj = datetime.date(ce_yr, mon, d_i)
                date_str = f"{d_i:02d}.{ENGLISH_MONTHS[mon - 1]}.{be_yr}"
            except ValueError:
                pass

    if date_str:
        if anchor_name:
            header_line = f"NEWSLINE {date_str} -- ANCHOR: {anchor_name}"
        else:
            header_line = f"NEWSLINE {date_str}"
    elif date_obj:
        date_str = f"{date_obj.day:02d}.{ENGLISH_MONTHS[date_obj.month - 1]}.{be_year(date_obj.year)}"
        if anchor_name:
            header_line = f"NEWSLINE {date_str} -- ANCHOR: {anchor_name}"
        else:
            header_line = f"NEWSLINE {date_str}"
    else:
        if anchor_name:
            header_line = f"NEWSLINE -- ANCHOR: {anchor_name}"
        else:
            header_line = "NEWSLINE"

    headlines: list[str] = []
    rich_runs: list[list[dict]] = []

    for sb in story_blocks:
        num = sb["num"]
        prefix = f"{num}. "
        if sb["th_lines"]:
            th_text = sb["th_lines"][0].strip()
            headline = f"{prefix}{th_text}"
            headlines.append(headline)
            runs = []
            if sb["th_paras"]:
                cur_off = len(prefix)
                for elem in sb["th_paras"][0].get("elements", []):
                    if "textRun" in elem:
                        tr = elem["textRun"]
                        c = tr.get("content", "").replace("\n", "").replace("\r", "")
                        st = {
                            k: v for k, v in tr.get("textStyle", {}).items()
                            if v and k in ("bold", "italic", "underline", "strikethrough", "foregroundColor", "backgroundColor", "fontSize", "weightedFontFamily")
                        }
                        if c:
                            if st:
                                runs.append({"start": cur_off, "end": cur_off + len(c), "style": st})
                            cur_off += len(c)
            rich_runs.append(runs)
        else:
            en_full = " ".join(sb["en_lines"]).strip()
            en = re.sub(r'^\[[^\]]+\]\s*', '', en_full)
            headline = f"{prefix}{en}"
            headlines.append(headline)
            runs = []
            if sb.get("en_para"):
                en_para = sb["en_para"]
                p_full = "".join(e.get("textRun", {}).get("content", "") for e in en_para.get("elements", []))
                idx = p_full.find(en)
                if idx != -1:
                    cur_p_pos = 0
                    for elem in en_para.get("elements", []):
                        if "textRun" in elem:
                            tr = elem["textRun"]
                            c = tr.get("content", "").replace("\n", "").replace("\r", "")
                            c_len = len(c)
                            c_start = cur_p_pos
                            c_end = cur_p_pos + c_len
                            cur_p_pos = c_end

                            ov_start = max(c_start, idx)
                            ov_end = min(c_end, idx + len(en))
                            if ov_start < ov_end:
                                st = {
                                    k: v for k, v in tr.get("textStyle", {}).items()
                                    if v and k in ("bold", "italic", "underline", "strikethrough", "foregroundColor", "backgroundColor", "fontSize", "weightedFontFamily")
                                }
                                if st:
                                    r_s = len(prefix) + (ov_start - idx)
                                    r_e = len(prefix) + (ov_end - idx)
                                    runs.append({"start": r_s, "end": r_e, "style": st})
            rich_runs.append(runs)

    thai_date_display = format_thai_western(date_obj) if date_obj else ""

    return {
        "doc_id": clean_id,
        "title": title,
        "date": date_obj.isoformat() if date_obj else None,
        "date_display": thai_date_display,
        "header": header_line,
        "header_date": date_str,
        "anchor": anchor_name,
        "headlines": headlines,
        "headline_count": len(headlines),
        "rich_runs": rich_runs,
    }


def parse_monthly_doc_blocks(content: list[dict]) -> list[dict]:
    """Parse existing monthly compilation doc structured content into sorted list of daily blocks.

    Extracts block ranges (startIndex, endIndex) and date for each day block.
    """
    blocks: list[dict] = []
    current_block: dict | None = None

    for el in content:
        if "paragraph" not in el:
            continue
        p = el["paragraph"]
        p_start = el.get("startIndex", 1)
        p_end = el.get("endIndex", p_start + 1)
        p_text = "".join(e.get("textRun", {}).get("content", "") for e in p.get("elements", []))
        date_obj = None

        # 1. Check dateElement in paragraph
        for e in p.get("elements", []):
            if "dateElement" in e:
                ts = e["dateElement"].get("dateElementProperties", {}).get("timestamp")
                if ts:
                    try:
                        date_obj = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                        break
                    except Exception:
                        pass

        # 2. Check regex in text (matches headers with or without -- ANCHOR:)
        if not date_obj and "NEWSLINE" in p_text:
            m = re.search(r'NEWSLINE\s+(\d{1,2})[\.|\s]([A-Za-zก-๙0-9]+)[\.|\s]([0-9]{2,4})', p_text, re.IGNORECASE)
            if m:
                d_i = int(m.group(1))
                m_s = m.group(2).upper()
                y_i = int(m.group(3))
                if m_s.isdigit():
                    m_i = int(m_s)
                else:
                    m_i = MONTH_MAP_EN.get(m_s, MONTH_MAP_TH.get(m_s, 1))
                if y_i < 100:
                    ce_y = 2000 + y_i
                    if ce_y > 2040:
                        ce_y = (2500 + y_i) - 543
                elif y_i > 2400:
                    ce_y = y_i - 543
                else:
                    ce_y = y_i
                try:
                    date_obj = datetime.date(ce_y, m_i, d_i)
                except ValueError:
                    pass

        if date_obj:
            if current_block:
                current_block["endIndex"] = p_start
                blocks.append(current_block)
            current_block = {
                "date": date_obj,
                "header_text": p_text.strip(),
                "startIndex": p_start,
                "endIndex": p_end,
                "headlines": [],
            }
        elif current_block:
            current_block["endIndex"] = p_end
            if p_text.strip():
                current_block["headlines"].append(p_text.strip())

    if current_block:
        blocks.append(current_block)

    blocks.sort(key=lambda b: b["date"])
    return blocks


def parse_monthly_doc_text(text: str) -> list[dict]:
    """Parse existing monthly compilation doc text into sorted list of daily blocks."""
    pattern = re.compile(r'(NEWSLINE\s+(\d{1,2})\.([A-Za-zก-๙]+)\.([0-9]{4})(?:\s*--\s*ANCHOR:?[^\n]*)?)', re.IGNORECASE)
    matches = list(pattern.finditer(text))
    blocks = []

    for i, m in enumerate(matches):
        full_header = m.group(1).strip()
        day = int(m.group(2))
        mon_str = m.group(3).upper()
        mon = MONTH_MAP_EN.get(mon_str, MONTH_MAP_TH.get(mon_str, 1))
        yr = int(m.group(4))
        ce_yr = yr - 543 if yr > 2400 else yr
        try:
            d_obj = datetime.date(ce_yr, mon, day)
        except ValueError:
            continue

        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block_body = text[start_pos:end_pos].strip()

        headlines = [line.strip() for line in block_body.split("\n") if line.strip()]

        blocks.append({
            "date": d_obj,
            "header": full_header,
            "headlines": headlines,
        })
    blocks.sort(key=lambda b: b["date"])
    return blocks


def format_monthly_doc_text(blocks: list[dict]) -> str:
    """Format daily blocks into complete monthly doc text."""
    blocks_sorted = sorted(blocks, key=lambda b: b["date"])
    parts = []
    for b in blocks_sorted:
        block_text = b["header"].strip() + " \n\n"
        for h in b["headlines"]:
            block_text += h + "\n"
        parts.append(block_text.strip())
    if not parts:
        return ""
    return "\n\n\n".join(parts) + "\n\n"


def build_day_block_requests(
    header_str: str,
    headlines: list[str],
    rich_runs_list: list[list[dict]] | None = None,
    insert_index: int = 1,
    tab_id: str | None = None,
    delete_range: tuple[int, int] | None = None,
) -> tuple[list[dict], str]:
    """Construct Docs API batchUpdate requests to write a day-block with full formatting.

    Formatting matches existing monthly blocks:
    - Header: TITLE, CENTER, bold, yellow background (#FFFF00), 11pt Tahoma
    - Spacer: NORMAL_TEXT
    - Headlines: NORMAL_TEXT, 11pt Tahoma, plus preserved source rich runs
    - End Spacer: NORMAL_TEXT
    """
    full_text = ""
    p_styles: list[dict] = []
    t_styles: list[dict] = []

    has_headlines = len(headlines) > 0

    # 1. Header paragraph
    h_line = f"{header_str.strip()} \n"
    h_start = len(full_text)
    h_end = h_start + len(h_line)
    full_text += h_line

    h_style: dict = {
        "namedStyleType": "TITLE",
        "alignment": "CENTER",
        "lineSpacing": 100,
        "direction": "LEFT_TO_RIGHT",
        "keepLinesTogether": True,
    }
    h_fields = "namedStyleType,alignment,lineSpacing,direction,keepLinesTogether"
    if has_headlines:
        h_style["keepWithNext"] = True
        h_fields += ",keepWithNext"

    p_styles.append({
        "start": h_start,
        "end": h_end,
        "style": h_style,
        "fields": h_fields,
    })
    t_styles.append({
        "start": h_start,
        "end": h_end - 1,  # exclude \n
        "style": {
            "bold": True,
            "backgroundColor": {"color": {"rgbColor": {"red": 1.0, "green": 1.0, "blue": 0.0}}},
            "fontSize": {"magnitude": 11, "unit": "PT"},
            "weightedFontFamily": {"fontFamily": "Tahoma", "weight": 400},
        },
        "fields": "bold,backgroundColor,fontSize,weightedFontFamily",
    })

    # 2. Header spacer paragraph
    sp1_start = len(full_text)
    full_text += "\n"
    sp1_end = len(full_text)
    sp1_style: dict = {
        "namedStyleType": "NORMAL_TEXT",
        "lineSpacing": 100,
        "direction": "LEFT_TO_RIGHT",
        "keepLinesTogether": True,
    }
    sp1_fields = "namedStyleType,lineSpacing,direction,keepLinesTogether"
    if has_headlines:
        sp1_style["keepWithNext"] = True
        sp1_fields += ",keepWithNext"

    p_styles.append({
        "start": sp1_start,
        "end": sp1_end,
        "style": sp1_style,
        "fields": sp1_fields,
    })

    # 3. Headlines
    for i, h in enumerate(headlines):
        line = f"{h.strip()}\n"
        l_start = len(full_text)
        l_end = l_start + len(line)
        full_text += line

        is_last = (i == len(headlines) - 1)
        hl_style: dict = {
            "namedStyleType": "NORMAL_TEXT",
            "lineSpacing": 100,
            "direction": "LEFT_TO_RIGHT",
            "keepLinesTogether": True,
        }
        hl_fields = "namedStyleType,lineSpacing,direction,keepLinesTogether"
        if not is_last:
            hl_style["keepWithNext"] = True
            hl_fields += ",keepWithNext"

        p_styles.append({
            "start": l_start,
            "end": l_end,
            "style": hl_style,
            "fields": hl_fields,
        })
        t_styles.append({
            "start": l_start,
            "end": l_end,
            "style": {
                "fontSize": {"magnitude": 11, "unit": "PT"},
                "weightedFontFamily": {"fontFamily": "Tahoma", "weight": 400},
            },
            "fields": "fontSize,weightedFontFamily",
        })

        if rich_runs_list and i < len(rich_runs_list):
            for rr in rich_runs_list[i]:
                st = rr.get("style", {})
                if st:
                    t_styles.append({
                        "start": l_start + rr["start"],
                        "end": l_start + rr["end"],
                        "style": st,
                        "fields": ",".join(st.keys()),
                    })

    # 4. End spacer paragraph
    sp2_start = len(full_text)
    full_text += "\n"
    sp2_end = len(full_text)
    p_styles.append({
        "start": sp2_start,
        "end": sp2_end,
        "style": {
            "namedStyleType": "NORMAL_TEXT",
            "lineSpacing": 100,
            "direction": "LEFT_TO_RIGHT",
            "keepLinesTogether": True,
        },
        "fields": "namedStyleType,lineSpacing,direction,keepLinesTogether",
    })

    reqs: list[dict] = []

    # Optional deletion of old day's block
    if delete_range and delete_range[1] > delete_range[0]:
        d_rg = {"startIndex": delete_range[0], "endIndex": delete_range[1]}
        if tab_id:
            d_rg["tabId"] = tab_id
        reqs.append({"deleteContentRange": {"range": d_rg}})

    # Insert day block text
    loc: dict = {"index": insert_index}
    if tab_id:
        loc["tabId"] = tab_id
    reqs.append({"insertText": {"location": loc, "text": full_text}})

    # Apply paragraph styles
    for ps in p_styles:
        rg = {"startIndex": insert_index + ps["start"], "endIndex": insert_index + ps["end"]}
        if tab_id:
            rg["tabId"] = tab_id
        reqs.append({
            "updateParagraphStyle": {
                "range": rg,
                "paragraphStyle": ps["style"],
                "fields": ps["fields"],
            }
        })

    # Apply text styles
    for ts in t_styles:
        rg = {"startIndex": insert_index + ts["start"], "endIndex": insert_index + ts["end"]}
        if tab_id:
            rg["tabId"] = tab_id
        reqs.append({
            "updateTextStyle": {
                "range": rg,
                "textStyle": ts["style"],
                "fields": ts["fields"],
            }
        })

    return reqs, full_text


def find_default_monthly_rundown_doc(target_date: datetime.date) -> dict | None:
    """Find the monthly 'รันดาวน์ MM/YYYY' compilation doc in the target FY folder."""
    fy = fy_be(target_date.year, target_date.month)
    folder_id, _, _ = find_or_create_fy_folder(DEST_ROOT_FOLDER, fy, dry=True)
    if not folder_id:
        return None
    month_name = THAI_MONTHS[target_date.month - 1]
    files = find_existing_files(folder_id)
    for f in files:
        fname = f.get("name", "")
        if fname.startswith("###"):
            continue
        if "รันดาวน์" in fname and month_name in fname:
            return {
                "id": f["id"],
                "name": f["name"],
                "url": f.get("webViewLink", f"https://docs.google.com/document/d/{f['id']}/edit"),
            }
    return None


def preview_rundown_fill(doc_id: str, monthly_doc_id: str | None = None) -> dict:
    """Extract daily rundown and preview changes to monthly compilation doc."""
    daily = extract_nl_rundown_from_doc(doc_id)
    if not daily["date"]:
        _fatal(f"Could not determine date for daily doc '{doc_id}'")

    date_obj = datetime.date.fromisoformat(daily["date"])
    monthly_info = None

    if monthly_doc_id and str(monthly_doc_id).strip():
        m_id = extract_doc_id(monthly_doc_id)
        m_meta = _api("GET", f"{DRIVE}/{m_id}", params={"fields": "id,name,webViewLink", "supportsAllDrives": "true"})
        monthly_info = {
            "id": m_meta.get("id", m_id),
            "name": m_meta.get("name", "Target Monthly Doc"),
            "url": m_meta.get("webViewLink", f"https://docs.google.com/document/d/{m_id}/edit"),
        }
    else:
        monthly_info = find_default_monthly_rundown_doc(date_obj)

    if not monthly_info:
        _fatal(f"Monthly compilation doc for {format_thai_western(date_obj)} not found — provide target monthly_doc_id")

    m_doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{monthly_info['id']}", params={"includeTabsContent": "true"})
    tabs = m_doc.get("tabs", [])
    if tabs:
        content = tabs[0].get("documentTab", {}).get("body", {}).get("content", [])
        if not content:
            content = tabs[0].get("body", {}).get("content", [])
    else:
        content = m_doc.get("body", {}).get("content", [])

    existing_blocks = parse_monthly_doc_blocks(content)
    existing_dates = [b["date"].isoformat() for b in existing_blocks]
    action = "replace" if daily["date"] in existing_dates else "insert"

    return {
        "dry_run": True,
        "daily_doc": daily,
        "headlines": daily["headlines"],
        "headline_count": daily["headline_count"],
        "target_monthly_doc": {
            **monthly_info,
            "existing_dates": existing_dates,
            "action": action,
        },
    }


def execute_rundown_fill(doc_id: str, monthly_doc_id: str | None = None, dry_run: bool = False) -> dict:
    """Extract daily rundown and write/replace day's block in monthly compilation doc with formatting preserved."""
    prev = preview_rundown_fill(doc_id, monthly_doc_id)
    if dry_run:
        return prev

    target = prev["target_monthly_doc"]
    m_id = target["id"]
    daily = prev["daily_doc"]
    date_obj = datetime.date.fromisoformat(daily["date"])

    m_doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{m_id}", params={"includeTabsContent": "true"})
    tabs = m_doc.get("tabs", [])
    if tabs:
        tab = tabs[0]
        tab_id = tab.get("tabProperties", {}).get("tabId")
        content = tab.get("documentTab", {}).get("body", {}).get("content", [])
        if not content:
            content = tab.get("body", {}).get("content", [])
    else:
        tab_id = None
        content = m_doc.get("body", {}).get("content", [])

    doc_end_idx = content[-1]["endIndex"] if content else 1

    existing_blocks = parse_monthly_doc_blocks(content)

    action = "inserted"
    del_range = None
    insert_index = 1

    matched = [b for b in existing_blocks if b["date"] == date_obj]
    if matched:
        action = "replaced"
        m_block = matched[0]
        del_start = m_block["startIndex"]
        del_end = min(m_block["endIndex"], max(del_start, doc_end_idx - 1))
        del_range = (del_start, del_end)
        insert_index = del_start
    else:
        action = "inserted"
        later_blocks = [b for b in existing_blocks if b["date"] > date_obj]
        if later_blocks:
            insert_index = later_blocks[0]["startIndex"]
        elif existing_blocks:
            insert_index = min(existing_blocks[-1]["endIndex"], max(1, doc_end_idx - 1))
        else:
            insert_index = 1

    requests, _ = build_day_block_requests(
        header_str=daily["header"],
        headlines=daily["headlines"],
        rich_runs_list=daily.get("rich_runs", []),
        insert_index=insert_index,
        tab_id=tab_id,
        delete_range=del_range,
    )

    if requests:
        _api("POST", f"https://docs.googleapis.com/v1/documents/{m_id}:batchUpdate", body={"requests": requests})

    total_days = len(existing_blocks) if action == "replaced" else (len(existing_blocks) + 1)

    return {
        "success": True,
        "dry_run": False,
        "daily_doc": daily,
        "headlines": daily["headlines"],
        "headline_count": daily["headline_count"],
        "target_monthly_doc": {
            "id": target["id"],
            "name": target["name"],
            "url": target["url"],
            "action": action,
            "total_days": total_days,
        },
    }


def parse_month_year_params(
    yyyymm: str | int | None = None,
    fy_be_val: str | int | None = None,
    month_val: str | int | None = None,
    year_val: str | int | None = None,
) -> tuple[int, int]:
    """Resolve CE year (int) and month (1-12) from various parameter schemes."""
    if yyyymm:
        s = str(yyyymm).strip().replace("-", "").replace("/", "")
        if len(s) == 6 and s.isdigit():
            y = int(s[:4])
            m = int(s[4:])
            if y > 2400:
                y -= 543
            if 1 <= m <= 12:
                return y, m
        _fatal(f"invalid yyyymm: '{yyyymm}' — use YYYYMM (e.g. 202608)")

    if fy_be_val is not None and month_val is not None:
        try:
            fy = int(str(fy_be_val).strip())
        except ValueError:
            _fatal(f"invalid fiscal year: '{fy_be_val}'")
        m_str = str(month_val).strip()
        if m_str.isdigit():
            m = int(m_str)
        else:
            m = MONTH_MAP_EN.get(m_str.upper(), MONTH_MAP_TH.get(m_str, 0))
        if not (1 <= m <= 12):
            _fatal(f"invalid month: '{month_val}'")
        ce_y = (fy - 543) - (1 if m >= 10 else 0)
        return ce_y, m

    if month_val is not None:
        m_str = str(month_val).strip().replace("-", "").replace("/", "")
        if len(m_str) == 6 and m_str.isdigit():
            return parse_month_year_params(yyyymm=m_str)
        if year_val is not None:
            try:
                y = int(str(year_val).strip())
                if y > 2400:
                    y -= 543
            except ValueError:
                _fatal(f"invalid year: '{year_val}'")
            if m_str.isdigit():
                m = int(m_str)
            else:
                m = MONTH_MAP_EN.get(m_str.upper(), MONTH_MAP_TH.get(m_str, 0))
            if 1 <= m <= 12:
                return y, m
        now = datetime.date.today()
        m = int(m_str) if m_str.isdigit() else MONTH_MAP_EN.get(m_str.upper(), MONTH_MAP_TH.get(m_str, 0))
        if 1 <= m <= 12:
            return now.year, m
        _fatal(f"invalid month: '{month_val}'")

    _fatal("yyyymm (e.g. 202608) or fy_be+month required")
    raise ValueError("Invalid parameters")


def find_matching_daily_docs_for_month(year: int, month: int) -> list[dict]:
    """Find available daily 'NL & NWB DDMMYY' docs for that month from Google Drive.
    Filters by DDMMYY matching target month and excludes weekends (Mon-Fri weekdays only).
    """
    target_mm = f"{month:02d}"
    target_yy_ce = f"{year % 100:02d}"
    target_yy_be = f"{(year + 543) % 100:02d}"

    files: list[dict] = []
    page = None
    while True:
        params = {
            "q": "name contains 'NL' and mimeType='application/vnd.google-apps.document' and trashed=false",
            "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "pageSize": "100",
        }
        if page:
            params["pageToken"] = page
        data = _api("GET", DRIVE, params=params)
        files.extend(data.get("files", []))
        page = data.get("nextPageToken")
        if not page or len(files) >= 300:
            break

    matched: list[dict] = []
    seen_dates: set[datetime.date] = set()

    for f in files:
        fname = f.get("name", "").strip()
        m = re.search(r'(\d{2})(\d{2})(\d{2})', fname)
        if not m:
            continue
        d_s, m_s, y_s = m.group(1), m.group(2), m.group(3)
        if m_s != target_mm:
            continue
        if y_s != target_yy_ce and y_s != target_yy_be:
            continue
        try:
            d_i = int(d_s)
            d_obj = datetime.date(year, month, d_i)
        except ValueError:
            continue

        if d_obj.weekday() >= 5:
            continue

        if d_obj in seen_dates:
            continue
        seen_dates.add(d_obj)

        matched.append({
            "id": f["id"],
            "name": fname,
            "url": f.get("webViewLink", f"https://docs.google.com/document/d/{f['id']}/edit"),
            "date": d_obj,
            "date_iso": d_obj.isoformat(),
            "date_display": format_thai_western(d_obj),
            "day": d_i,
            "modified": f.get("modifiedTime", ""),
        })

    matched.sort(key=lambda x: x["date"])
    return matched


def preview_month_rundown_fill(year: int, month: int, monthly_doc_id: str | None = None) -> dict:
    """Preview all matching daily docs and planned insertions/replacements for the whole month."""
    sample_date = datetime.date(year, month, 1)
    monthly_info = None

    if monthly_doc_id and str(monthly_doc_id).strip():
        m_id = extract_doc_id(monthly_doc_id)
        m_meta = _api("GET", f"{DRIVE}/{m_id}", params={"fields": "id,name,webViewLink", "supportsAllDrives": "true"})
        monthly_info = {
            "id": m_meta.get("id", m_id),
            "name": m_meta.get("name", "Target Monthly Doc"),
            "url": m_meta.get("webViewLink", f"https://docs.google.com/document/d/{m_id}/edit"),
        }
    else:
        monthly_info = find_default_monthly_rundown_doc(sample_date)

    if not monthly_info:
        _fatal(f"Monthly compilation doc for {THAI_MONTHS[month - 1]} {be_year(year)} not found — provide target monthly_doc_id")

    m_doc = _api("GET", f"https://docs.googleapis.com/v1/documents/{monthly_info['id']}", params={"includeTabsContent": "true"})
    tabs = m_doc.get("tabs", [])
    if tabs:
        content = tabs[0].get("documentTab", {}).get("body", {}).get("content", [])
        if not content:
            content = tabs[0].get("body", {}).get("content", [])
    else:
        content = m_doc.get("body", {}).get("content", [])

    existing_blocks = parse_monthly_doc_blocks(content)
    existing_dates = [b["date"].isoformat() for b in existing_blocks]

    matching_docs = find_matching_daily_docs_for_month(year, month)
    matching_with_actions = []
    for d in matching_docs:
        action = "replace" if d["date_iso"] in existing_dates else "insert"
        matching_with_actions.append({
            "id": d["id"],
            "name": d["name"],
            "url": d["url"],
            "date": d["date_iso"],
            "date_iso": d["date_iso"],
            "date_display": d["date_display"],
            "day": d["day"],
            "action": action,
        })

    return {
        "dry_run": True,
        "month": f"{year}{month:02d}",
        "month_display": f"{THAI_MONTHS[month - 1]} {be_year(year)}",
        "year": year,
        "month_num": month,
        "fy_be": fy_be(year, month),
        "target_monthly_doc": {
            **monthly_info,
            "existing_dates": existing_dates,
            "total_existing_days": len(existing_blocks),
        },
        "matching_docs": matching_with_actions,
        "counts": {
            "total_matched": len(matching_docs),
            "to_insert": sum(1 for d in matching_with_actions if d["action"] == "insert"),
            "to_replace": sum(1 for d in matching_with_actions if d["action"] == "replace"),
        },
    }


def execute_month_rundown_fill(year: int, month: int, monthly_doc_id: str | None = None, dry_run: bool = False) -> dict:
    """Run execute_rundown_fill for each matching daily doc in the target month.
    Idempotent per day.
    """
    prev = preview_month_rundown_fill(year, month, monthly_doc_id)
    if dry_run:
        return prev

    target = prev["target_monthly_doc"]
    m_id = target["id"]
    matching_docs = prev.get("matching_docs", [])

    days_filled = []
    days_skipped = []

    for d in matching_docs:
        try:
            res = execute_rundown_fill(d["id"], monthly_doc_id=m_id, dry_run=False)
            days_filled.append({
                "date": d["date_iso"],
                "date_display": d["date_display"],
                "doc_id": d["id"],
                "doc_name": d["name"],
                "headlines": res.get("headline_count", len(res.get("headlines", []))),
                "action": res.get("target_monthly_doc", {}).get("action", "inserted"),
            })
        except Exception as e:
            days_skipped.append({
                "date": d["date_iso"],
                "doc_id": d["id"],
                "doc_name": d["name"],
                "reason": str(e),
            })

    return {
        "success": True,
        "dry_run": False,
        "month": prev["month"],
        "month_display": prev["month_display"],
        "year": year,
        "month_num": month,
        "fy_be": prev["fy_be"],
        "target_monthly_doc": {
            "id": target["id"],
            "name": target["name"],
            "url": target["url"],
        },
        "days_filled": days_filled,
        "days_skipped": days_skipped,
        "counts": {
            "total_matched": len(matching_docs),
            "filled": len(days_filled),
            "skipped": len(days_skipped),
        },
    }


def list_recent_daily_docs(limit: int = 15) -> list[dict]:
    """List recent daily 'NL & NWB' script docs from Drive for picker."""
    data = _api("GET", DRIVE, params={
        "q": "name contains 'NL' and mimeType='application/vnd.google-apps.document' and trashed=false",
        "fields": "files(id,name,modifiedTime,webViewLink)",
        "orderBy": "modifiedTime desc",
        "pageSize": str(limit),
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    })
    files = data.get("files", [])
    res = []
    for f in files:
        fname = f.get("name", "")
        if "NL" in fname.upper() and ("NWB" in fname.upper() or "RUNDOWN" in fname.upper() or re.search(r'\d{4,6}', fname)):
            res.append({
                "id": f["id"],
                "name": fname,
                "url": f.get("webViewLink", f"https://docs.google.com/document/d/{f['id']}/edit"),
                "modified": f.get("modifiedTime", ""),
            })
    return res


# --- Sub-tab ③: NL Document Generator (Bulk 36 Docs for FY) -------------

MONTHS_FY_ORDER = [
    {"period": 1, "month": 10, "name_th": "ตุลาคม", "year_offset": -1},
    {"period": 2, "month": 11, "name_th": "พฤศจิกายน", "year_offset": -1},
    {"period": 3, "month": 12, "name_th": "ธันวาคม", "year_offset": -1},
    {"period": 4, "month": 1, "name_th": "มกราคม", "year_offset": 0},
    {"period": 5, "month": 2, "name_th": "กุมภาพันธ์", "year_offset": 0},
    {"period": 6, "month": 3, "name_th": "มีนาคม", "year_offset": 0},
    {"period": 7, "month": 4, "name_th": "เมษายน", "year_offset": 0},
    {"period": 8, "month": 5, "name_th": "พฤษภาคม", "year_offset": 0},
    {"period": 9, "month": 6, "name_th": "มิถุนายน", "year_offset": 0},
    {"period": 10, "month": 7, "name_th": "กรกฎาคม", "year_offset": 0},
    {"period": 11, "month": 8, "name_th": "สิงหาคม", "year_offset": 0},
    {"period": 12, "month": 9, "name_th": "กันยายน", "year_offset": 0},
]


def build_docgen_plan(fy_be: int) -> list[dict]:
    """Generate plan for 36 docs (12 months × 3 templates) for given fiscal year (BE)."""
    periods = []
    for info in MONTHS_FY_ORDER:
        p = info["period"]
        p_str = f"{p:02d}"
        cal_be = fy_be + info["year_offset"]
        m_name = info["name_th"]

        cover_name = f"{p_str} ใบรายงานผลการปฏิบัติงาน แบบ QR Code {m_name} {cal_be} ณอรรฆย์ โรจนสุวรรณ.docx"
        log_name = f"{p_str} รายงานผลการปฏิบัติงาน {m_name} {cal_be}.docx"
        rundown_name = f"{p_str} รันดาวน์ {m_name} {cal_be}"

        docs = [
            {"type": "cover", "name": cover_name, "template_id": TEMPLATE_COVER},
            {"type": "log", "name": log_name, "template_id": TEMPLATE_LOG},
            {"type": "rundown", "name": rundown_name, "template_id": TEMPLATE_RUNDOWN},
        ]
        periods.append({
            "period": p_str,
            "period_num": p,
            "month_num": info["month"],
            "month_thai": m_name,
            "cal_be_year": cal_be,
            "docs": docs,
        })
    return periods


def preview_docgen(fy_be: int | str) -> dict:
    """Preview 36 planned documents and check which ones already exist in target FY folder."""
    try:
        fy = int(str(fy_be).strip())
    except Exception:
        _fatal(f"invalid fiscal year: '{fy_be}'")
        raise

    folder_id, folder_name, folder_created = find_or_create_fy_folder(DEST_ROOT_FOLDER, fy, dry=True)
    plan_periods = build_docgen_plan(fy)

    existing_files_map = {}
    if folder_id:
        files = find_existing_files(folder_id)
        for f in files:
            fname = f.get("name", "").strip()
            if not fname.startswith("###"):
                existing_files_map[fname] = f

    total_docs = 0
    existing_count = 0
    to_create_count = 0

    for p in plan_periods:
        for d in p["docs"]:
            total_docs += 1
            d_name = d["name"]
            if d_name in existing_files_map:
                ex = existing_files_map[d_name]
                d["exists"] = True
                d["id"] = ex["id"]
                d["url"] = ex.get("webViewLink", f"https://docs.google.com/document/d/{ex['id']}/edit")
                existing_count += 1
            else:
                d["exists"] = False
                to_create_count += 1

    return {
        "dry_run": True,
        "fy_be": fy,
        "folder": {
            "id": folder_id,
            "name": folder_name,
            "exists": bool(folder_id),
        },
        "periods": plan_periods,
        "total_planned": total_docs,
        "existing_count": existing_count,
        "to_create_count": to_create_count,
    }


def generate_bulk_docs(fy_be: int | str, dry_run: bool = False) -> dict:
    """Duplicate 3 templates × 12 months = 36 docs for fiscal year into target FY folder.
    Idempotent: skips docs that already exist by name.
    """
    try:
        fy = int(str(fy_be).strip())
    except Exception:
        _fatal(f"invalid fiscal year: '{fy_be}'")
        raise

    if dry_run:
        return preview_docgen(fy)

    folder_id, folder_name, folder_created = find_or_create_fy_folder(DEST_ROOT_FOLDER, fy, dry=False)
    if not folder_id:
        _fatal(f"Failed to find or create folder for FY {fy}")

    plan_periods = build_docgen_plan(fy)
    existing_files = find_existing_files(folder_id)
    existing_map = {f.get("name", "").strip(): f for f in existing_files if not f.get("name", "").startswith("###")}

    created = []
    skipped = []

    for p in plan_periods:
        for d in p["docs"]:
            d_name = d["name"]
            if d_name in existing_map:
                ex = existing_map[d_name]
                skipped.append({
                    "name": d_name,
                    "id": ex["id"],
                    "url": ex.get("webViewLink", f"https://docs.google.com/document/d/{ex['id']}/edit"),
                    "type": d["type"],
                    "period": p["period"],
                })
            else:
                new_id, new_name, new_link = copy_file(d["template_id"], d_name, folder_id)
                created.append({
                    "name": new_name,
                    "id": new_id,
                    "url": new_link,
                    "type": d["type"],
                    "period": p["period"],
                })

    return {
        "success": True,
        "dry_run": False,
        "fy_be": fy,
        "folder": {
            "id": folder_id,
            "name": folder_name,
        },
        "created": created,
        "skipped": skipped,
        "total_planned": 36,
        "created_count": len(created),
        "skipped_count": len(skipped),
    }


# --- CLI entrypoint ------------------------------------------------------


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    # Subcommand routing
    if argv and argv[0] == "daily-docs":
        p = argparse.ArgumentParser(description="List recent daily docs")
        p.add_argument("--limit", type=int, default=15)
        args = p.parse_args(argv[1:])
        res = list_recent_daily_docs(limit=args.limit)
        print(json.dumps(res, ensure_ascii=False))
        return

    if argv and argv[0] == "rundown":
        p = argparse.ArgumentParser(description="NEWSLINE Rundown extraction and monthly doc fill")
        p.add_argument("command", nargs="?", choices=["fill", "preview", "fill-month", "preview-month"], default="fill")
        p.add_argument("--doc-id", default=None, help="Daily doc ID or URL")
        p.add_argument("--monthly-doc-id", default=None, help="Target monthly doc ID (optional)")
        p.add_argument("--month", default=None, help="Target month YYYYMM (e.g. 202608) or month number (1-12)")
        p.add_argument("--fy-be", default=None, help="Fiscal year BE (e.g. 2569)")
        p.add_argument("--year", default=None, help="CE year (e.g. 2026)")
        p.add_argument("--dry-run", action="store_true", help="Dry run preview mode")
        args = p.parse_args(argv[1:])
        dry = args.dry_run or args.command in ("preview", "preview-month")

        if args.command in ("fill-month", "preview-month") or (args.month and not args.doc_id):
            y, m = parse_month_year_params(yyyymm=args.month, fy_be_val=args.fy_be, month_val=args.month, year_val=args.year)
            res = execute_month_rundown_fill(y, m, monthly_doc_id=args.monthly_doc_id, dry_run=dry)
            print(json.dumps(res, ensure_ascii=False))
            return

        if not args.doc_id:
            _fatal("doc-id is required for single day fill (or use fill-month with --month)")

        res = execute_rundown_fill(args.doc_id, args.monthly_doc_id, dry_run=dry)
        print(json.dumps(res, ensure_ascii=False))
        return

    if argv and argv[0] == "docgen":
        p = argparse.ArgumentParser(description="NEWSLINE Document Generator (bulk 36 docs for FY)")
        p.add_argument("command", nargs="?", choices=["generate", "preview"], default="generate")
        p.add_argument("--fy-be", required=True, help="Fiscal year BE (e.g. 2569)")
        p.add_argument("--dry-run", action="store_true", help="Dry run preview mode")
        args = p.parse_args(argv[1:])
        dry = args.dry_run or args.command == "preview"
        res = generate_bulk_docs(args.fy_be, dry_run=dry)
        print(json.dumps(res, ensure_ascii=False))
        return

    # Default / legacy monthly-reports generator
    if argv and argv[0] == "monthly-report":
        argv = argv[1:]

    p = argparse.ArgumentParser(description="NEWSLINE Reports generator.")
    p.add_argument("--period", required=True, help="Period no. (งวดที่, e.g. 5 or 11)")
    p.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="Preview mode (no writes)")
    p.add_argument("command", nargs="?", choices=["generate", "preview"], default="generate")
    args = p.parse_args(argv)

    start_d = parse_date(args.start)
    end_d = parse_date(args.end)
    dry = args.dry_run or args.command == "preview"

    res = generate_reports(args.period, start_d, end_d, dry_run=dry)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()

