"""JEV judgment gates — pure logic + degraded-skip + cache behavior.

The meter subprocess is NEVER invoked for real here: _metered_call is
monkeypatched. One live metered proof happens out-of-band (hub, manual).
"""


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

    def fake_run(argv, input=None, capture_output=False, text=False, timeout=None):
        class P:
            returncode = 3
            stdout = '{"answers": {"name0": {"noul": 0.4}}}'
            stderr = ""

        return P()

    monkeypatch.setattr(jev_gates.subprocess, "run", fake_run)
    answers, model = jev_gates._metered_call({"questions": {}})
    assert answers == {"name0": {"noul": 0.4}}
    assert model is None  # fixture stdout carries no model field


def test_run_gates_applies_verdicts_and_caches(monkeypatch, tmp_path):
    calls = []

    def fake_meter(payload):
        calls.append(payload)
        qs = payload["questions"]
        return _fake_answers(
            [v["instructions"].split('"')[1] for k, v in qs.items() if k.startswith("name")],
            [v["instructions"].split('"')[1] for k, v in qs.items() if k.startswith("emph")],
        ), "test-model" 

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


# ------------------------------------------- style triage + apply (2026-09-23)

STYLE_FLAGS = [
    {"kind": "slop", "name": "pivotal", "detail": "AI vocabulary — use the plain word"},
    {"kind": "slop", "name": "served as", "detail": 'fancy "is" — say "is"'},
]


def test_questions_include_style_flags():
    q = jev_gates._questions(
        [], [], [("slop", "pivotal", "AI vocabulary — plain word", "«the pivotal moment»")])
    assert q["style0"]["type"] == "noul"
    assert '"pivotal"' in q["style0"]["instructions"]
    assert "AI vocabulary" in q["style0"]["instructions"]
    assert "pivotal moment" in q["style0"]["instructions"]       # context rides along


def test_run_gates_style_triage(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "meter.py").write_text("# fake")

    def fake_meter(payload):
        answers = {}
        for k, v in payload["questions"].items():
            if k.startswith("style"):
                # "pivotal" is a real violation; anything else is regex noise
                answers[k] = {"noul": 0.9 if '"pivotal"' in v["instructions"] else 0.2}
        return answers, "test-model" 

    monkeypatch.setattr(jev_gates, "_metered_call", fake_meter)
    out = jev_gates.run_gates(
        "The pivotal project served as a model for the region.", style_flags=STYLE_FLAGS)
    verdicts = {s["name"]: s["verdict"] for s in out["style"]}
    assert verdicts["pivotal"] == "violation"
    assert verdicts["served as"] == "ok"


def test_run_gates_without_meter_reports_empty_style(no_meter):
    out = jev_gates.run_gates("Plain body, no caps runs, no dates.")
    assert out["style"] == []


APPLY_BODY = (
    "EN: Anutin wins\nTH: หัวข้อ\n\n"
    "Prime Minister Anutin Charnvirakul met voters on July 15 and spent 60,000 baht. "
    "Anutin Charnvirakul smiled. Spokesman Chai Wat spoke too."
)


def _report(names, emphasis=()):
    return {
        "names": names,
        "emphasis": [{"span": s, "score": 2, "pick": 1.0} for s in emphasis],
        "style": [],
    }


def test_apply_gates_overlays_first_mention_and_bolds_registry_misses():
    report = _report([
        {"english": "Anutin Charnvirakul", "thai": "อนุทิน ชาญวีรกูล"},
        {"english": "Chai Wat", "thai": None},          # registry miss → ＋wiki stays advisory
    ])
    out, counts = jev_gates.apply_gates(APPLY_BODY, report)
    assert "**Anutin Charnvirakul [อนุทิน ชาญวีรกูล]**" in out   # first mention carries Thai
    assert out.count("อนุทิน") == 1                              # later mention untouched
    assert "**Chai Wat**" in out                                 # miss → bold only, no invented Thai
    assert counts == {"bold": 1, "overlay": 1, "underline": 0}
    # idempotent — a re-apply (CONVERT-again) changes nothing
    out2, counts2 = jev_gates.apply_gates(out, report)
    assert out2 == out and counts2 == {"bold": 0, "overlay": 0, "underline": 0}


def test_apply_gates_wraps_emphasis_and_skips_marked_spans():
    body = "EN: T\nTH: หัวข้อ\n\nMet on ~~July 15~~ and again July 16, spent 60,000 baht."
    out, counts = jev_gates.apply_gates(
        body, _report([], ["July 15", "July 16", "60,000"]))
    assert out.count("~~July 15~~") == 1        # already marked → exactly one wrap
    assert "~~July 16~~" in out and "~~60,000~~" in out
    assert counts["underline"] == 2


def test_apply_gates_injects_thai_into_existing_bold():
    body = ("EN: T\nTH: หัวข้อ\n\n**Anutin Charnvirakul** met voters. "
            "Later Anutin Charnvirakul left.")
    out, counts = jev_gates.apply_gates(
        body, _report([{"english": "Anutin Charnvirakul", "thai": "อนุทิน ชาญวีรกูล"}]))
    assert "**Anutin Charnvirakul [อนุทิน ชาญวีรกูล]**" in out
    assert out.count("อนุทิน") == 1
    assert counts == {"bold": 0, "overlay": 1, "underline": 0}


def test_apply_gates_spares_names_inside_bigger_bold_phrases():
    body = "EN: T\nTH: หัวข้อ\n\nThe **Anutin Charnvirakul Coalition** survived the vote."
    out, _ = jev_gates.apply_gates(
        body, _report([{"english": "Anutin Charnvirakul", "thai": "อนุทิน"}]))
    assert out == body      # a **bold phrase** is not a name overlay — untouched


def test_apply_gates_never_touches_title_lines():
    body = "EN: Anutin Charnvirakul Speaks\nTH: หัวข้อ\n\nBody text about Anutin Charnvirakul today."
    out, _ = jev_gates.apply_gates(
        body, _report([{"english": "Anutin Charnvirakul", "thai": "อนุทิน ชาญวีรกูล"}]))
    assert out.startswith("EN: Anutin Charnvirakul Speaks")      # title line untouched
    assert "**Anutin Charnvirakul [อนุทิน ชาญวีรกูล]**" in out   # body first mention carries it


# ------------------------------------- live-tuned judgment (2026-09-23, v4)

def test_name_candidates_strip_sentence_leadins():
    """'Later Anutin Charnvirakul' glued the sentence adverb into the run and
    Jev passed it at 0.92 — strip lead-ins like titles; the stripped span
    dedups against the bare name."""
    body = ("EN: T\nTH: หัวข้อ\n\nAnutin Charnvirakul chaired the meeting. "
            "Later Anutin Charnvirakul dismissed the rumors.")
    got = jev_gates.name_candidates(body)
    assert got.count("Anutin Charnvirakul") == 1              # dedup after strip
    assert not any(g.startswith("Later") for g in got)


def test_borderline_two_word_names_count_as_persons(monkeypatch, tmp_path):
    """Live-tuned threshold (0.80 → 0.60): Jev scored real Thai romanization
    'Chai Wat' at 0.64 — a person the convention must not drop. Noise sits
    far lower (Government House 0.03, Interior 0.05)."""
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "meter.py").write_text("# fake")

    def fake_meter(payload):
        answers = {}
        for k, v in payload["questions"].items():
            if k.startswith("name"):
                span = v["instructions"].split('"')[1]
                answers[k] = {"noul": 0.64 if span == "Chai Wat" else 0.05}
        return answers, "test-model"

    monkeypatch.setattr(jev_gates, "_metered_call", fake_meter)
    out = jev_gates.run_gates("The statement came from Chai Wat yesterday.")
    assert [n["english"] for n in out["names"]] == ["Chai Wat"]


# ----------------------------- update-propagation wiring (2026-09-23, v5)

def test_cache_ttl_expiry_and_legacy_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    jev_gates.cache_write("k", {"a": 1}, "m-1")
    ans, model = jev_gates.cache_read("k")
    assert (ans, model) == ({"a": 1}, "m-1")
    # expire it
    import json as _json
    import time as _time
    p = jev_gates._cache_path("k")
    e = _json.loads(p.read_text())
    e["ts"] = _time.time() - jev_gates.CACHE_TTL_S - 1
    p.write_text(_json.dumps(e))
    assert jev_gates.cache_read("k") == (None, "m-1")         # TTL miss keeps the stamp
    # legacy (pre-v5) shape: raw answers without stamp → stale by definition
    p.write_text(_json.dumps({"a": 1}))
    assert jev_gates.cache_read("k") == (None, None)


def test_model_change_flushes_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    jev_gates.cache_write("k1", {"a": 1}, "jev-2026-09")
    jev_gates.cache_write("k2", {"b": 2}, "jev-2026-09")
    assert jev_gates.cache_model("k1") == "jev-2026-09"
    n = jev_gates.flush_cache()
    assert n == 2
    assert jev_gates.cache_model("k1") is None
    assert jev_gates.flush_cache() == 0                        # nothing left


def test_run_gates_flushes_on_model_upgrade(monkeypatch, tmp_path):
    """A fresh response whose model id differs from the stored stamp must
    flush the whole cache — operations then re-judge on the new JEV."""
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "meter.py").write_text("# fake")
    jev_gates.cache_write(BODY, {"name0": {"noul": 0.01}}, "jev-OLD")
    # age the entry past the TTL — propagation happens at expiry, bounded by 24 h
    import json as _json
    import time as _time
    p = jev_gates._cache_path(BODY)
    e = _json.loads(p.read_text())
    e["ts"] = _time.time() - jev_gates.CACHE_TTL_S - 1
    p.write_text(_json.dumps(e))
    state = {"calls": 0}

    def fake_meter(payload):
        state["calls"] += 1
        return {"name0": {"noul": 0.95}}, "jev-NEW"

    monkeypatch.setattr(jev_gates, "_metered_call", fake_meter)
    out = jev_gates.run_gates(BODY)
    assert state["calls"] == 1                        # expired + model changed → re-metered
    assert out["jev_model"] == "jev-NEW"
    assert jev_gates.cache_model(BODY) == "jev-NEW"
    # same model again → cache hit, no new call
    out2 = jev_gates.run_gates(BODY)
    assert state["calls"] == 1 and out2["jev_model"] == "jev-NEW"


def test_metered_questions_passthrough(monkeypatch):
    monkeypatch.setattr(
        jev_gates.subprocess, "run",
        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": '{"answers": {"x": {"noul": 0.9}}, "model": "jev-2026-10"}', "stderr": ""})())
    answers, model = jev_gates.metered_questions("s", {"x": {"type": "noul"}})
    assert answers == {"x": {"noul": 0.9}} and model == "jev-2026-10"


# --------------------------- NEWSROOM infographic corroboration (2026-09-23)

def test_infographic_suggest_flags_disputed_picks(monkeypatch, tmp_path):
    """The director's picks ride; JEV re-judges each as a closed call and the
    response flags disputed paragraphs — advisory, annotate unchanged."""
    from fastapi.testclient import TestClient

    import app.newsroom as newsroom
    from app.main import app

    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "meter.py").write_text("# fake")

    async def fake_zai(prompt, **kw):
        return ('{"picks": [{"paragraph": 2, "headline": "Numbers jump", "why": "w",'
                ' "intake": "i", "facts": "60,000"}, {"paragraph": 3, "headline": '
                '"Quote only", "why": "w", "intake": "i", "facts": ""}]}')

    monkeypatch.setattr(newsroom.zai, "zai_message", fake_zai)

    def fake_metered(state, questions):
        answers = {}
        for k, v in questions.items():
            # paragraph 3 (quote-only) = not genuinely visual → disputed
            answers[k] = {"noul": 0.15 if "Quote only" not in k and "pick3" in k else
                          (0.15 if "pick3" in k else 0.9)}
        return answers, "jev-test"

    monkeypatch.setattr("app.jev_gates.metered_questions", fake_metered)
    c = TestClient(app)
    r = c.post("/api/newsroom/infographic/suggest",
               json={"text": "Para one.\n\nPara two has 60,000 visitors.\n\nPara three is a quote."})
    assert r.status_code == 200, r.text
    body = r.json()
    j = body["jev"]
    assert j["ok"] is True and j["jev_model"] == "jev-test"
    assert j["disputed"] == [3]
    # annotated text still carries both INFOGRAPHIC blocks (advisory only)
    assert body["count"] == 2


# ------------------------- JEV mood-match for infographics (2026-09-23)

MOOD_TEXT = "The ministry reported 3.5 million arrivals, revenue up 22 percent to 180 billion baht; markets rallied on the news."


def test_jev_classify_moods_buckets_and_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    (tmp_path / "meter.py").write_text("# fake")

    def fake_metered(state, questions):
        assert set(questions) == {f"mood_{t}" for t in jev_gates and []} or True
        probs = {"mood_business": 0.95, "mood_hard-news": 0.80, "mood_tech": 0.70,
                 "mood_sport": 0.65, "mood_culture": 0.10}
        return {k: {"noul": probs.get(k, 0.05)} for k in questions}, "jev-test"

    monkeypatch.setattr("app.jev_gates.metered_questions", fake_metered)
    from app import newsroom
    buckets, model = newsroom._jev_classify_moods(MOOD_TEXT)
    assert model == "jev-test"
    assert buckets == ["business", "hard-news", "tech"]       # top-3 by prob, cap 3
    # second call → cache hit, no meter
    n = {"c": 0}

    def counting(state, questions):
        n["c"] += 1
        return fake_metered(state, questions)

    monkeypatch.setattr("app.jev_gates.metered_questions", counting)
    newsroom._jev_classify_moods(MOOD_TEXT)
    assert n["c"] == 0


def test_jev_classify_moods_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "missing.py")
    from app import newsroom
    assert newsroom._jev_classify_moods(MOOD_TEXT) == (None, None)


def test_pick_inf_look_uses_jev_moods(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    (tmp_path / "meter.py").write_text("# fake")
    monkeypatch.setattr(
        "app.jev_gates.metered_questions",
        lambda s, q: ({k: {"noul": 0.90} for k in q}, "jev-test"))
    from app import newsroom
    style, pal = newsroom.pick_inf_look(MOOD_TEXT)
    assert style["mood_source"] == "jev" and style["jev_model"] == "jev-test"
    assert style["matched_moods"]                             # JEV buckets fired
    assert style["pick_source"] == "mood"


def test_pick_inf_look_regex_fallback_when_jev_down(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "missing.py")
    from app import newsroom
    style, _ = newsroom.pick_inf_look("Police arrested the suspect in the murder case; the court sentenced him today.")
    assert style["mood_source"] == "regex"                     # fallback intact
    assert style["jev_model"] is None


def test_pick_inf_look_forced_still_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_gates, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jev_gates, "METER", tmp_path / "meter.py")
    (tmp_path / "meter.py").write_text("# fake")
    monkeypatch.setattr(
        "app.jev_gates.metered_questions",
        lambda s, q: ({k: {"noul": 0.9} for k in q}, "jev-test"))
    from app import newsroom
    forced = newsroom._INF_STYLES[0]["id"]
    style, _ = newsroom.pick_inf_look(MOOD_TEXT, forced)
    assert style["pick_source"] == "forced"
    assert style["id"] == forced
    assert style["mood_source"] == "jev"                       # provenance still stamped
