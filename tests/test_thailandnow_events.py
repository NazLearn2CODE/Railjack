"""EVENTS IDE CONVERT lane — dedup/merge logic + the /events/convert handoff reader.

Covers the two pieces task 3 added (office ref Somatic b92bfc1, implement-not-copy):
  * `_dedup_merge_events` — multi-source dupes collapse into one row keeping all URLs;
  * `convert_ide_events` — reads /tmp/thailand-now-events/latest.json, soft-fails when
    missing/unparseable, else returns window-filtered + deduped + start_date-sorted events.

No HTTP client / no anyio plugin: the async handler is driven directly via stdlib
``asyncio.run``, and the handoff path is steered off real /tmp by monkeypatching the
module-level ``_IDE_HANDOFF`` constant (same pattern as ``_SCOUT_HANDOFF``)."""
import asyncio
import json
from datetime import datetime, timedelta

from app import thailandnow
from app.thailandnow import _dedup_merge_events, convert_ide_events

NOW = datetime.now()
ISO = lambda dt: dt.strftime("%Y-%m-%d")


def test_dedup_merge_collapses_same_event_across_sources():
    # Two listings of the same expo (long base title → source tag falls outside the
    # 30-char prefix key, so they key-match) must merge into one row keeping both URLs.
    rows = [
        {"title": "Bangkok International Tech Expo 2026 (TAT)",
         "url": "https://tat.or.th/expo", "urls": ["https://tat.or.th/expo"],
         "start_date": ISO(NOW + timedelta(days=5))},
        {"title": "Bangkok International Tech Expo 2026 (AllConf)",
         "url": "https://allconf.com/expo", "urls": ["https://allconf.com/expo"],
         "start_date": ISO(NOW + timedelta(days=5))},
    ]
    out = _dedup_merge_events(rows, ISO(NOW), ISO(NOW + timedelta(weeks=4)), "ide")
    assert len(out) == 1
    expo = out[0]
    assert expo["source"] == "ide"
    assert expo["url"] == "https://tat.or.th/expo"            # first non-empty stays
    assert set(expo["urls"]) == {"https://tat.or.th/expo", "https://allconf.com/expo"}


def test_dedup_merge_drops_past_and_out_of_window():
    rows = [
        {"title": "Past Festival", "url": "https://e.com/past",
         "start_date": ISO(NOW - timedelta(days=5))},
        {"title": "Way Future Summit", "url": "https://e.com/far",
         "start_date": ISO(NOW + timedelta(weeks=10))},
        {"title": "In-Window Jazz Fest", "url": "https://jazz.th/cm",
         "start_date": ISO(NOW + timedelta(days=10))},
    ]
    out = _dedup_merge_events(rows, ISO(NOW), ISO(NOW + timedelta(weeks=4)), "ide")
    assert len(out) == 1
    assert out[0]["title"] == "In-Window Jazz Fest"


def test_convert_soft_fails_when_handoff_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(thailandnow, "_IDE_HANDOFF", tmp_path / "does-not-exist.json")
    res = asyncio.run(convert_ide_events({}))
    assert res == {"events": [], "count": 0,
                   "errors": ["no IDE handoff file — run 📋 IDE SCOUT first"]}


def test_convert_reads_dedupes_and_sorts_valid_handoff(tmp_path, monkeypatch):
    in_window = ISO(NOW + timedelta(days=5))
    later = ISO(NOW + timedelta(days=10))
    handoff = {
        "generated_at": ISO(NOW),
        "window": {"weeks": 4},
        "events": [
            {"title": "Bangkok International Tech Expo 2026 (TAT)",
             "url": "https://tat.or.th/expo", "urls": ["https://tat.or.th/expo"],
             "start_date": in_window, "location": "Bangkok", "summary": "Tech expo."},
            {"title": "Bangkok International Tech Expo 2026 (AllConf)",
             "url": "https://allconf.com/expo", "urls": ["https://allconf.com/expo"],
             "start_date": in_window, "summary": "Dupe listing."},
            {"title": "Past Fest", "url": "https://e.com/past",
             "start_date": ISO(NOW - timedelta(days=5)), "summary": "filtered."},
            {"title": "Chiang Mai Jazz Fest", "url": "https://jazz.th/cm",
             "urls": ["https://jazz.th/cm", "https://cmnews.th/jazz"],
             "start_date": later, "summary": "Jazz in CM."},
        ],
    }
    f = tmp_path / "latest.json"
    f.write_text(json.dumps(handoff), encoding="utf-8")
    monkeypatch.setattr(thailandnow, "_IDE_HANDOFF", f)

    res = asyncio.run(convert_ide_events({"weeks": 4}))
    assert res["errors"] == []
    assert res["count"] == 2
    assert [e["start_date"] for e in res["events"]] == [in_window, later]   # ascending
    expo, jazz = res["events"]
    assert expo["source"] == "ide"
    assert set(expo["urls"]) == {"https://tat.or.th/expo", "https://allconf.com/expo"}
    assert jazz["urls"] == ["https://jazz.th/cm", "https://cmnews.th/jazz"]


def test_convert_soft_fails_on_unparseable_handoff(tmp_path, monkeypatch):
    f = tmp_path / "latest.json"
    f.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(thailandnow, "_IDE_HANDOFF", f)
    res = asyncio.run(convert_ide_events({}))
    assert res == {"events": [], "count": 0,
                   "errors": ["no IDE handoff file — run 📋 IDE SCOUT first"]}


def test_parse_fb_events_real_shape_bold_unicode():
    # verbatim structure from opencli facebook search (2026-08-17 probe)
    rows = [
        {"title": "Events", "url": "https://www.facebook.com/events/1805396480911086/",
         "text": ("Events FRI, AUG 28 AT 11 AM AND 2 MORE"
                  "𝗕𝗮𝗻𝗴𝗸𝗼𝗸 𝗜𝗻𝘁𝗲𝗿𝗻𝗮𝘁𝗶𝗼𝗻𝗮𝗹 𝗚𝗮𝗺𝗲 𝗙𝗲𝘀𝘁𝗶𝘃𝗮𝗹 𝟮𝟬𝟮𝟲\n"
                  "28 people interested · 8th floor of")},
        {"title": "Pages", "url": "https://www.facebook.com/x", "text": "noise"},
    ]
    out = thailandnow._parse_fb_events(json.dumps(rows), "2026-08-17", "2026-09-14")
    assert len(out) == 1
    ev = out[0]
    assert ev["title"] == "Bangkok International Game Festival 2026"  # NFKC de-bolded
    assert ev["start_date"] == "2026-08-28"
    assert ev["url"].endswith("1805396480911086/")
    # out-of-window dates are dropped
    assert thailandnow._parse_fb_events(json.dumps(rows), "2026-10-01", "2026-11-01") == []
