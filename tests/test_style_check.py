"""Ben-voice / anti-slop checks (app.style_check) — rule 14 as code.

Errors are objective (dashes, prose parens, unbalanced markers, non-prose
markup); warnings are heuristics Naz arbitrates (slop vocab, rhythm, shape,
person, quotes). Overlay brackets holding Thai are EXEMPT from the paren
error — they are functional formatting, not prose.
"""

from app.style_check import check_style


def _blob(body: str) -> str:
    """Wrap a body in the EN/TH title pair (title zone is exempt from checks)."""
    return f"EN: t\nTH: หัวข้อ\n\n{body}"


def test_clean_blob_passes_with_no_warnings():
    p1 = (
        "Flooding swept through the market district of Chiang Mai on ~~Tuesday~~, "
        "damaging more than 200 stalls along the Ping river. City officials "
        "moved vendors to higher ground within hours and closed three riverside roads "
        "as the water kept rising. **Anutin Charnvirakul [อนุทิน ชาญวีรกูล]** chaired "
        "an emergency meeting by video link and ordered fast payments for the "
        "displaced stall owners. Provincial crews began pumping water from the lowest "
        "arcade before dawn on ~~Wednesday~~."
    )
    p2 = (
        "The Chiang Mai chamber of commerce estimates losses at 40 million baht "
        "and asked the city to waive market fees for a month. Shop owners stacked "
        "sandbags along the arcade entrances while volunteers carried goods to the "
        "upper floor of the market hall. Forecasters expect the river to fall below "
        "the warning level by the weekend, though more rain is likely in the "
        "northern highlands. Two shelters at a nearby school held about sixty "
        "families overnight, and the city handed out hot meals through the morning. "
        "The market reopens on ~~Friday~~ if the ground dries, and the chamber asked "
        "vendors to check electrical panels before restocking freezers and fridges."
    )
    out = check_style(_blob(p1 + "\n\n" + p2))
    assert out["ok"], out["errors"]
    assert out["warnings"] == [], out["warnings"]
    assert 180 <= out["stats"]["words"] <= 250
    assert out["stats"]["sentences"] >= 4


def test_em_dash_and_prose_parentheses_are_errors():
    out = check_style(_blob("The plan — unveiled Monday — stalled (again) today."))
    assert not out["ok"]
    kinds = [e["kind"] for e in out["errors"]]
    assert "dash" in kinds and "paren" in kinds


def test_parens_inside_overlay_are_exempt():
    """Parens inside Thai-bearing BRACKETS are functional formatting."""
    out = check_style(
        _blob("Minister **[Anutin Charnvirakul(อนุทิน ชาญวีรกูล)]** spoke to reporters.")
    )
    assert not any(e["kind"] == "paren" for e in out["errors"])


def test_prose_paren_outside_overlay_still_errors():
    out = check_style(_blob("The council (all twelve members) voted unanimously."))
    assert any(e["kind"] == "paren" for e in out["errors"])


def test_unbalanced_markers_are_errors():
    out = check_style(_blob("The **mayor spoke and the session ended."))
    assert not out["ok"]
    assert any(e["kind"] == "markers" for e in out["errors"])


def test_bullet_list_in_body_is_error():
    out = check_style(_blob("The plan:\n- first item\n- second item"))
    assert not out["ok"]
    assert any(e["kind"] == "markup" for e in out["errors"])


def test_slop_vocabulary_and_constructions_warn():
    body = (
        "The project is not just a renovation but a complete reimagining, and "
        "officials utilize it in order to showcase the city, a testament to "
        "careful planning."
    )
    out = check_style(_blob(body))
    assert out["ok"]  # advisory, not an error
    kinds = [w["kind"] for w in out["warnings"]]
    assert kinds.count("slop") >= 3


def test_long_sentence_word_count_and_paragraphs_warn():
    long_sentence = "The " + " ".join(["word"] * 30) + " ended."
    out = check_style(_blob(long_sentence))  # single paragraph → shape warnings too
    kinds = [w["kind"] for w in out["warnings"]]
    assert "long-sentence" in kinds
    assert "word-count" in kinds and "paragraphs" in kinds


def test_first_person_and_curly_quotes_warn():
    out = check_style(_blob("Our reporters saw the scene, and we counted “three” vans."))
    kinds = [w["kind"] for w in out["warnings"]]
    assert "first-person" in kinds and "quotes" in kinds


def test_spelled_numbers_warn():
    body = (
        "The ministry paid sixty thousand baht for sixty-nine cameras. "
        "Officials confirmed 3.2 million visitors and 205 billion dollars in trade."
    )
    res = check_style(_blob(body))
    nums = [w for w in res["warnings"] if w["kind"] == "number"]
    flagged = " | ".join(w["name"] for w in nums)
    assert "sixty thousand" in flagged, flagged
    assert "sixty-nine" in flagged, flagged
    assert "3.2 million" not in flagged and "205 billion" not in flagged, flagged


def test_numerals_clean_no_number_warning():
    body = "Thailand will ship 60,000 tons of rice worth 69 million dollars."
    res = check_style(_blob(body))
    assert not [w for w in res["warnings"] if w["kind"] == "number"]
