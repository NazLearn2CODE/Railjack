"""Thai-name fact-check (app.name_check) — the 2026-08-27 name rule.

Naz's spec: every Thai person/place name rides as **English Name [ชื่อไทย]**;
bare Thai in the body is an error; bracketed names should be registry-verified
(advisory warning when missing); overlays never carry honorific titles.
"""

import asyncio
import json
from pathlib import Path

from app import newsroom
from app.name_check import check_rewritten

_REG_NOTE = """---
title: "Anutin Charnvirakul"
date: 2026-08-27
english: Anutin Charnvirakul
thai: อนุทิน ชาญวีรกูล
kind: person
---

# Anutin Charnvirakul (อนุทิน ชาญวีรกูล)
"""


def _registry(tmp_path: Path) -> Path:
    d = tmp_path / "name-wiki"
    d.mkdir()
    (d / "anutin-charnvirakul.md").write_text(_REG_NOTE, encoding="utf-8")
    return d


def test_title_pair_exempt_and_bracketed_name_ok(tmp_path):
    """TH title line is exempt; a bracketed registered name verifies clean."""
    blob = (
        "EN: Buri Ram votes\n"
        "TH: บุรีรัมย์\n"
        "\n"
        "**Anutin Charnvirakul [อนุทิน ชาญวีรกูล]** visited **Nong Bun Mak district"
        " [อำเภอโนนบุรำ]**."
    )
    out = check_rewritten(blob, _registry(tmp_path))
    assert out["ok"], out["errors"]
    assert out["names"]["verified"] == ["อนุทิน ชาญวีรกูล"]
    # the unregistered place still verifies ok (advisory warning only)
    assert out["names"]["unverified"] == ["อำเภอโนนบุรำ"]
    assert any(w["kind"] == "unverified" for w in out["warnings"])


def test_bare_thai_in_body_is_error():
    blob = "EN: t\nTH: หัวข้อ\n\nนายกอนุทิน spoke to **Anutin [อนุทิน]** today."
    out = check_rewritten(blob)
    assert not out["ok"]
    assert any(e["thai"].startswith("นายก") for e in out["errors"])
    assert all("context" in e for e in out["errors"])


def test_honorific_in_overlay_warns(tmp_path):
    blob = "EN: t\nTH: ห\n\n**Anutin [นายอนุทิน ชาญวีรกูล]** spoke."
    out = check_rewritten(blob, _registry(tmp_path))
    assert out["ok"]  # advisory, not an error
    assert any(w["kind"] == "honorific" for w in out["warnings"])


def test_missing_registry_dir_warns_but_ok():
    out = check_rewritten("EN: t\nTH: h\n\n**Anutin [อนุทิน]**.", Path("/nonexistent/reg"))
    assert out["ok"]
    assert any(w["kind"] == "registry" for w in out["warnings"])
    assert out["names"]["unverified"] == ["อนุทิน"]


def test_convert_relays_namecheck_without_blocking(tmp_path, monkeypatch):
    """CONVERT surfaces the namecheck advisory but still relays the script."""
    handoff = tmp_path / "latest.json"
    handoff.write_text(
        json.dumps(
            {
                "rewritten": "EN: T\nTH: หัวข้อ\n\nนายอนุทิน spoke in Nakhon Pathom [นครปฐม].",
                "seo": "SEO",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(newsroom, "_REWRITE_HANDOFF", handoff)
    out = asyncio.run(newsroom.rewrite_convert())
    assert out["rewritten"]  # relayed — show, don't block
    assert not out["namecheck"]["ok"]  # bare Thai flagged
    assert any(e["thai"].startswith("นายอนุทิน") for e in out["namecheck"]["errors"])


def test_convert_clean_handoff_reports_ok(tmp_path, monkeypatch):
    handoff = tmp_path / "latest.json"
    handoff.write_text(
        json.dumps(
            {"rewritten": "EN: T\nTH: หัวข้อ\n\nClean body, no Thai.", "seo": "S"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(newsroom, "_REWRITE_HANDOFF", handoff)
    out = asyncio.run(newsroom.rewrite_convert())
    assert out["namecheck"]["ok"] is True


def test_strip_fabricated_thai_keeps_new_overlay(tmp_path):
    """The metered guard must keep Thai-bearing [...] brackets (new 2026-08-27
    format) while still unwrapping orphaned non-Thai brackets."""
    src = "อนุทิน ชาญวีรกูล แถลง"
    body = "Anutin **Anutin Charnvirakul [อนุทิน ชาญวีรกูล]** met [the council]."
    out = newsroom._strip_fabricated_thai(body, src)
    assert "[อนุทิน ชาญวีรกูล]" in out, out  # overlay brackets survive
    assert "the council" in out and "[the council]" not in out  # orphan unwrap still works

def test_legacy_bracket_verifies_thai_part(tmp_path):
    """Legacy `[English(ไทย)]` shape: registry lookup uses the Thai part and the
    readable form carries the parens."""
    blob = "EN: t\nTH: ห\n\n**[Anutin Charnvirakul(อนุทิน ชาญวีรกูล)]** spoke."
    out = check_rewritten(blob, _registry(tmp_path))
    assert out["ok"], out["warnings"]
    assert out["names"]["verified"] == ["Anutin Charnvirakul (อนุทิน ชาญวีรกูล)"]
    assert out["names"]["unverified"] == []


def test_legacy_bracket_english_mismatch_warns(tmp_path):
    """Registry hit but the bracket's english spelling differs → warning."""
    blob = "EN: t\nTH: ห\n\n**[Anutin Charnveerakul(อนุทิน ชาญวีรกูล)]** spoke."
    out = check_rewritten(blob, _registry(tmp_path))
    assert out["names"]["verified"] == ["Anutin Charnveerakul (อนุทิน ชาญวีรกูล)"]
    assert any(w["kind"] == "english-mismatch" for w in out["warnings"])


def test_legacy_bracket_unverified_shows_readable_form():
    blob = "EN: t\nTH: ห\n\n**[Arada Fuangtong(อารดา เฟื่องทอง)]** spoke."
    out = check_rewritten(blob)
    assert out["names"]["unverified"] == ["Arada Fuangtong (อารดา เฟื่องทอง)"]


def test_surname_only_later_mention_warns():
    """Thai convention (Naz 2026-09-26): later mentions use the GIVEN name,
    never the family name — a bare surname after the first mention warns."""
    blob = (
        "EN: t\nTH: ห\n\n"
        "**Anutin Charnvirakul [อนุทิน ชาญวีรกูล]** spoke. Later, Charnvirakul added."
    )
    out = check_rewritten(blob)
    assert out["ok"]  # advisory, never an error
    hit = [w for w in out["warnings"] if w["kind"] == "surname-mention"]
    assert len(hit) == 1
    assert hit[0]["name"] == "Charnvirakul"
    assert "Anutin" in hit[0]["detail"]


def test_given_name_later_mention_stays_clean():
    """Given-name and full-name repeats are the correct forms — no warning."""
    blob = (
        "EN: t\nTH: ห\n\n"
        "**Anutin Charnvirakul [อนุทิน ชาญวีรกูล]** spoke. Later, Anutin added. "
        "Prime Minister Anutin Charnvirakul repeated the pledge."
    )
    out = check_rewritten(blob)
    assert not any(w["kind"] == "surname-mention" for w in out["warnings"])


def test_surname_check_covers_legacy_parens_overlay():
    blob = (
        "EN: t\nTH: ห\n\n"
        "**[Anutin Charnvirakul(อนุทิน ชาญวีรกูล)]** spoke. Charnvirakul left."
    )
    out = check_rewritten(blob)
    assert any(w["kind"] == "surname-mention" for w in out["warnings"])


def test_place_overlay_never_surname_checked():
    """Places repeat in full — the surname scan skips place-word overlays."""
    blob = (
        "EN: t\nTH: ห\n\n"
        "**Nong Bun Mak district [อำเภอโนนบุรำ]** votes today."
    )
    out = check_rewritten(blob)
    assert not any(w["kind"] == "surname-mention" for w in out["warnings"])


def test_english_source_name_not_surname_checked():
    """A non-Thai name (no Thai in the bracket zone) is exempt — Western style
    legitimately reuses the family name."""
    blob = "EN: t\nTH: ห\n\n**Donald Trump** spoke. Trump added."
    out = check_rewritten(blob)
    assert not any(w["kind"] == "surname-mention" for w in out["warnings"])
