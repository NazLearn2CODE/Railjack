"""TN SEO JEV gates (seo_jev) — bulk auto-skip, advisory insert, degrade.

The meter is NEVER invoked for real: metered_questions is monkeypatched.
"""

import pytest

from app import jev_gates, seo_jev


PICKS = [
    {"host_id": 11, "host_title": "Khon Kaen Food Guide", "phrase": "Khon Kaen street food",
     "count": 1, "snippet": "..."},
    {"host_id": 22, "host_title": "Isaan Weaving Villages", "phrase": "street food tours",
     "count": 1, "snippet": "..."},
    {"host_id": 33, "host_title": "No phrase here", "matches": 0},  # noop — ungated
]


@pytest.fixture()
def fresh_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    (tmp_path / "meter.py").write_text("# fake")


def test_gate_bulk_picks_skips_failed_pair(fresh_cache, monkeypatch):
    """pair+anchor noul below threshold → host auto-skipped in bulk runs."""
    calls = []

    def fake_metered(state, questions):
        calls.append(questions)
        answers = {}
        for k, v in questions.items():
            noul = 0.90
            if k.startswith("pair") and "Weaving" in v["instructions"]:
                noul = 0.10  # host 22 — unrelated topics
            if k.startswith("anchor") and "tours" in v["instructions"]:
                noul = 0.30  # host 22 — spammy phrase
            answers[k] = {"noul": noul}
        return answers, "jev-test"

    monkeypatch.setattr("app.jev_gates.metered_questions", fake_metered)
    gate_map, meta = seo_jev.gate_bulk_picks("Street Food Guide",
                                             "https://x/khon-kaen/", PICKS)
    assert gate_map["11"] is True
    assert gate_map["22"] is False
    assert "33" not in gate_map                    # noop — nothing to gate
    assert meta["model"] == "jev-test" and meta["gated"] == 2
    assert len(calls) == 1                         # ONE metered call for the orphan


def test_gate_bulk_picks_degrades_on_jev_failure(fresh_cache, monkeypatch):
    """Jev down → empty map (no gate) + skipped reason; NEVER raises."""
    monkeypatch.setattr("app.jev_gates.metered_questions",
                        lambda s, q: (_ for _ in ()).throw(RuntimeError("meter down")))
    gate_map, meta = seo_jev.gate_bulk_picks("T", "https://x/t/", PICKS)
    assert gate_map == {}
    assert "skipped" in meta


def test_gate_bulk_picks_uses_cache(fresh_cache, monkeypatch):
    calls = []

    def fake_metered(state, questions):
        calls.append(1)
        return {k: {"noul": 0.9} for k in questions}, "jev-test"

    monkeypatch.setattr("app.jev_gates.metered_questions", fake_metered)
    seo_jev.gate_bulk_picks("T", "https://x/t/", PICKS)
    seo_jev.gate_bulk_picks("T", "https://x/t/", PICKS)
    assert len(calls) == 1                         # second run billed nothing


def test_gate_bulk_picks_flushes_on_model_upgrade(fresh_cache, monkeypatch):
    """JEV upgraded since the cached verdicts? flush + re-judge on the new model."""
    state = {"n": 0}

    def fake_metered(state_, questions):
        state["n"] += 1
        return {k: {"noul": 0.9} for k in questions}, f"jev-v{state['n']}"

    monkeypatch.setattr("app.jev_gates.metered_questions", fake_metered)
    import json as _json
    import time as _time
    seo_jev.gate_bulk_picks("T", "https://x/t/", PICKS)
    # age every cache entry past the TTL
    for p in jev_gates.CACHE_DIR.glob("*.json"):
        e = _json.loads(p.read_text())
        e["ts"] = _time.time() - jev_gates.CACHE_TTL_S - 1
        p.write_text(_json.dumps(e))
    seo_jev.gate_bulk_picks("T", "https://x/t/", PICKS)
    assert state["n"] == 2                         # expired → re-judged on the new model


def test_verdict_insert_advisory(fresh_cache, monkeypatch):
    monkeypatch.setattr(
        "app.jev_gates.metered_questions",
        lambda s, q: ({"ins0": {"noul": 0.85}}, "jev-test"))
    out = seo_jev.verdict_insert("Khon Kaen street food", "https://x/khon/",
                                 "Food Guide", "Visit ... Khon Kaen street food ...")
    assert out["verdict"] == "ok" and out["prob"] >= 0.6 and out["model"] == "jev-test"
    monkeypatch.setattr(
        "app.jev_gates.metered_questions",
        lambda s, q: ({"ins0": {"noul": 0.20}}, "jev-test"))
    out2 = seo_jev.verdict_insert("click here cheap pills", "https://x/khon/",
                                  "Other Page", "text")
    assert out2["verdict"] == "weak"


def test_verdict_insert_degrades(fresh_cache, monkeypatch):
    monkeypatch.setattr("app.jev_gates.metered_questions",
                        lambda s, q: (_ for _ in ()).throw(RuntimeError("down")))
    out = seo_jev.verdict_insert("p", "https://x/", "H", "excerpt")
    assert out["verdict"] == "skipped"


def test_verdict_insert_cache_hit_stamps_model(fresh_cache, monkeypatch):
    """Regression (live-found 2026-09-23): a cache hit must still report the
    judging model — UnboundLocalError surfaced as verdict 'skipped'."""
    calls = []

    def fake_metered(state, questions):
        calls.append(1)
        return {"ins0": {"noul": 0.85}}, "jev-1.13.0"

    monkeypatch.setattr("app.jev_gates.metered_questions", fake_metered)
    args = ("phrase p", "https://x/t/", "Host", "excerpt text")
    first = seo_jev.verdict_insert(*args)
    second = seo_jev.verdict_insert(*args)
    assert len(calls) == 1
    assert first["verdict"] == second["verdict"] == "ok"
    assert second["model"] == "jev-1.13.0"          # was the crash
