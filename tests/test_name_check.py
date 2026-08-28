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
