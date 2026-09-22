"""JEV judgment gates — pure logic + degraded-skip + cache behavior.

The meter subprocess is NEVER invoked for real here: _metered_call is
monkeypatched. One live metered proof happens out-of-band (hub, manual).
"""

import json

import pytest

from app import jev_gates


BODY = (
    "EN: Test headline\nTH: หัวข้อ\n\n"
    "Prime Minister Anutin Charnvirakul met reporters in Bangkok on ~~July 15, 2026~~. "
    "The agency counted 60,000 visitors next month, said spokesman Chai Wat."
)


def test_name_candidates_finds_capitalized_runs():
    got = jev_gates.name_candidates(BODY)
    assert "Anutin Charnvirakul" in got
    assert "Chai Wat" in got


def test_name_candidates_skips_title_lines_and_thai_brackets():
    body = "EN: Bangkok Post Report\n\nMinister **Somchai Jaidee [สมชาย ใจดี]** spoke."
    got = jev_gates.name_candidates(body)
    assert "Somchai Jaidee" not in got      # inside a Thai bracket overlay
    assert all("EN:" not in g for g in got)


def test_emphasis_candidates_finds_bare_dates_and_numbers():
    got = jev_gates.emphasis_candidates(BODY)
    assert any("60,000" in g for g in got)
    assert any("next month" in g for g in got)


def test_emphasis_candidates_skips_underlined_spans():
    body = "A ~~July 15, 2026~~ date and a bare 3:00 PM time."
    got = jev_gates.emphasis_candidates(body)
    assert not any("July 15" in g for g in got)
    assert any("3:00" in g for g in got)


@pytest.fixture()
def no_meter(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "missing.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")


def test_run_gates_without_meter_degrades_to_skipped(no_meter):
    out = jev_gates.run_gates(BODY)
    assert out["ok"] is True
    assert "skipped" in out
    assert out["names"] == [] and out["emphasis"] == []


def test_run_gates_meter_failure_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(
        jev_gates, "_metered_call", lambda payload: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = jev_gates.run_gates(BODY)
    assert out["ok"] is True and "skipped" in out


def _fake_answers(names, emphasis):
    answers = {}
    for i, n in enumerate(names):
        answers[f"name{i}"] = (
            {"noul": 0.95} if "Anutin" in n or "Chai" in n else {"noul": 0.10}
        )
    # Jev scores on the criteria-INDEX scale (3 anchors → 0..2)
    for i, e in enumerate(emphasis):
        answers[f"emph{i}"] = (
            {"score": 2} if ("60,000" in e or "July" in e) else {"score": 0}
        )
    return answers


def test_metered_call_parses_stdout_despite_low_confidence_exit(monkeypatch):
    """askjev exits 3 (low confidence) but still returns valid answers JSON —
    the exit code must never discard a parseable stdout (live-found gotcha)."""
    import subprocess as sp

    def fake_run(argv, input=None, capture_output=False, text=False, timeout=None):
        class P:
            returncode = 3
            stdout = '{"answers": {"name0": {"noul": 0.4}}}'
            stderr = ""

        return P()

    monkeypatch.setattr(jev_gates.subprocess, "run", fake_run)
    answers = jev_gates._metered_call({"questions": {}})
    assert answers == {"name0": {"noul": 0.4}}


def test_run_gates_applies_verdicts_and_caches(monkeypatch, tmp_path):
    calls = []

    def fake_meter(payload):
        calls.append(payload)
        qs = payload["questions"]
        return {"answers": _fake_answers(
            [v["instructions"].split('"')[1] for k, v in qs.items() if k.startswith("name")],
            [v["instructions"].split('"')[1] for k, v in qs.items() if k.startswith("emph")],
        )}

    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "meter.py").write_text("# fake meter")  # exists() gate must pass
    monkeypatch.setattr(jev_gates, "_metered_call", fake_meter)
    registry = {"anutin charnvirakul": "อนุทิน ชาญวีรกูล"}

    out1 = jev_gates.run_gates(BODY, registry=registry)
    assert out1["ok"] is True
    by_en = {n["english"]: n for n in out1["names"]}
    assert by_en["Anutin Charnvirakul"]["thai"] == "อนุทิน ชาญวีรกูล"
    assert by_en["Chai Wat"]["thai"] is None          # registry miss → flag
    assert any("60,000" in e["span"] for e in out1["emphasis"])
    assert all(e["pick"] >= jev_gates.PICK_NORMALIZED for e in out1["emphasis"])

    out2 = jev_gates.run_gates(BODY, registry=registry)   # cache hit
    assert len(calls) == 1                                # second call billed nothing
    assert out2["names"] == out1["names"]


def test_payload_shape_matches_askjev_contract():
    q = jev_gates._questions(["Some One"], [("July 15", "The festival opens July 15.")])
    assert q["name0"]["type"] == "noul" and "PERSON" in q["name0"]["instructions"]
    assert q["emph0"]["type"] == "score" and len(q["emph0"]["criteria"]) == 3
    assert '"July 15"' in q["emph0"]["instructions"] and "excerpt" in q["emph0"]["instructions"]


def test_registry_map_inverts_name_check_loader(monkeypatch, tmp_path):
    import app.name_check as nc

    wiki = tmp_path / "name-wiki"
    wiki.mkdir()
    (wiki / "a.md").write_text(
        '---\ntitle: "A"\nenglish: "Anutin Charnvirakul"\nthai: "อนุทิน"\n---\nbody',
        encoding="utf-8",
    )
    (wiki / "b.md").write_text(
        '---\ntitle: "B"\nthai: "เฉพาะไทย"\n---\nbody',  # no english → dropped
        encoding="utf-8",
    )
    monkeypatch.setattr(nc, "DEFAULT_REGISTRY_DIR", wiki)
    got = jev_gates.load_registry_map()
    assert got == {"anutin charnvirakul": "อนุทิน"}
