"""Tests for newsroom infographic pipeline (NotebookLM CLI integration,
parser/stripper, brief composition, TV-safe spec, watermark crop, endpoints).
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.newsroom import (
    _INF_PALETTES,
    _INF_STYLES,
    _INF_STYLES_BY_ID,
    _TV_SAFE_SPEC,
    _classify_moods,
    _compose_briefs,
    _crop_watermark,
    _strip_inf,
    _template_brief,
    generate_infographics,
    parse_inf_blocks,
    pick_inf_look,
)


# ── parse_inf_blocks ───────────────────────────────────────────────────


def test_parse_inf_blocks_zero_blocks():
    blocks, warnings = parse_inf_blocks("Just plain text with no markers.")
    assert blocks == []
    assert warnings == []


def test_parse_inf_blocks_one_block():
    text = (
        "Introductory sentence.\n\n"
        "[inf]\n"
        "Thailand saw **15 million** visitors by ~~July 2026~~.\n"
        "[inf/]\n\n"
        "Closing remarks."
    )
    blocks, warnings = parse_inf_blocks(text)
    assert blocks == ["Thailand saw **15 million** visitors by ~~July 2026~~."]
    assert warnings == []


def test_parse_inf_blocks_two_blocks():
    text = (
        "[inf]\n"
        "First visual block with $10M revenue.\n"
        "[inf/]\n"
        "Middle narrative paragraph.\n"
        "[inf]\n"
        "Second visual block with 45% growth.\n"
        "[inf/]"
    )
    blocks, warnings = parse_inf_blocks(text)
    assert len(blocks) == 2
    assert blocks[0] == "First visual block with $10M revenue."
    assert blocks[1] == "Second visual block with 45% growth."
    assert warnings == []


def test_parse_inf_blocks_unclosed_block():
    text = (
        "Headline\n"
        "[inf]\n"
        "Unclosed visual content running to the end.\n"
        "Second line of unclosed block."
    )
    blocks, warnings = parse_inf_blocks(text)
    assert len(blocks) == 1
    assert "Unclosed visual content" in blocks[0]
    assert "Second line" in blocks[0]
    assert len(warnings) == 1
    assert "unclosed" in warnings[0].lower()


def test_parse_inf_blocks_empty_block_warning():
    text = "[inf]\n   \n[inf/]"
    blocks, warnings = parse_inf_blocks(text)
    assert blocks == []
    assert len(warnings) == 1
    assert "empty" in warnings[0].lower()


def test_parse_inf_blocks_inner_text_preserved_with_markup():
    text = (
        "[inf]\n"
        "Minister **Suriya Juangroongruangkit** announced the ~~August 2026~~ plan.\n"
        "[inf/]"
    )
    blocks, warnings = parse_inf_blocks(text)
    assert blocks == [
        "Minister **Suriya Juangroongruangkit** announced the ~~August 2026~~ plan."
    ]
    assert warnings == []


# ── inline markers + typo tolerance (Naz, 2026-09-02) ───────────────────


def test_parse_inf_blocks_inline_single_line():
    blocks, warnings = parse_inf_blocks(
        "Intro line.\n[inf] Revenue hit 45 billion baht. [inf/]\nOutro line."
    )
    assert blocks == ["Revenue hit 45 billion baht."]
    assert warnings == []


def test_parse_inf_blocks_missing_slash_closer_saves_content():
    """Naz's exact typo: closing with [inf] instead of [inf/]. Content must
    survive; warnings point at the mistake."""
    text = "[inf]\nContent A with 100 baht.\n[inf]\n"
    blocks, warnings = parse_inf_blocks(text)
    assert blocks == ["Content A with 100 baht."]
    assert any("auto-closing" in w for w in warnings)
    assert any("ran to end" in w for w in warnings)


def test_parse_inf_blocks_inline_typo_one_line():
    text = "[inf] content content content [inf]"
    blocks, warnings = parse_inf_blocks(text)
    assert blocks == ["content content content"]
    assert any("auto-closing" in w for w in warnings)


def test_parse_inf_blocks_mixed_shapes():
    text = (
        "[inf] First inline block with 10 units. [inf/]\n"
        "narrative\n"
        "[inf]\n"
        "Second block with 20 units.\n"
        "[inf/]"
    )
    blocks, warnings = parse_inf_blocks(text)
    assert blocks == ["First inline block with 10 units.", "Second block with 20 units."]
    assert warnings == []


# ── _strip_inf ──────────────────────────────────────────────────────────


def test_strip_inf_removes_markers_and_keeps_text():
    text = (
        "Top headline.\n\n"
        "[inf]\n"
        "Middle graphic paragraph.\n"
        "[inf/]\n\n"
        "Bottom text."
    )
    stripped = _strip_inf(text)
    assert "[inf]" not in stripped
    assert "[inf/]" not in stripped
    assert "Middle graphic paragraph." in stripped
    assert "Top headline." in stripped
    assert "Bottom text." in stripped


def test_strip_inf_idempotent():
    text = "[inf]\nParagraph 1\n[inf/]\nParagraph 2"
    once = _strip_inf(text)
    twice = _strip_inf(once)
    assert once == twice
    assert "[inf]" not in once


def test_strip_inf_unmarked_text_unchanged():
    text = "Unmarked script.\nSecond line."
    assert _strip_inf(text) == text


def test_strip_inf_inline_markers_removed_text_kept():
    text = "Head [inf] middle content [inf/] tail."
    assert _strip_inf(text) == "Head middle content tail."


def test_strip_inf_typo_closer_is_stripped():
    # a bare [inf] (Naz's typo) must not leak into served text either
    text = "[inf]\nContent A.\n[inf]\nTail."
    assert _strip_inf(text) == "Content A.\nTail."
    # idempotent
    assert _strip_inf(_strip_inf(text)) == _strip_inf(text)


# ── _template_brief ─────────────────────────────────────────────────────


def test_template_brief_content():
    block = "Export revenue jumped by 14.5% reaching $300 billion in 2026. Non-digit sentence here."
    style = _INF_STYLES[0]  # flat-navy
    palette = next(p for p in _INF_PALETTES if p["id"] == "abyss-navy")
    brief = _template_brief(block, style, palette)

    assert style["art"] in brief
    assert "PALETTE (LOCKED):" in brief
    assert "#0B1F3A" in brief and "#56B4E9" in brief  # palette owns the colors
    assert "14.5%" in brief
    assert "$300 billion in 2026" in brief
    assert "80%" in brief
    assert "no logos" in brief.lower()
    assert "Show ONLY these figures (verbatim):" in brief


# ── _crop_watermark ─────────────────────────────────────────────────────


def test_crop_watermark_2752x1536_to_2752x1508(tmp_path):
    img_path = tmp_path / "synthetic_1536.png"
    img = Image.new("RGB", (2752, 1536), color=(255, 255, 255))
    # Fill bottom-right corner watermark region (y: 1515..1535, x: 2700..2750)
    for x in range(2700, 2750):
        for y in range(1515, 1535):
            img.putpixel((x, y), (0, 0, 0))
    img.save(img_path)

    _crop_watermark(img_path)

    with Image.open(img_path) as cropped:
        assert cropped.size == (2752, 1508)
        assert cropped.width == 2752
        assert cropped.height == 1508


def test_crop_watermark_1000x1000_to_1000x972(tmp_path):
    img_path = tmp_path / "synthetic_1000.png"
    img = Image.new("RGB", (1000, 1000), color=(255, 255, 255))
    img.save(img_path)

    _crop_watermark(img_path)

    with Image.open(img_path) as cropped:
        assert cropped.size == (1000, 972)


def test_crop_watermark_missing_file_raises(tmp_path):
    missing_path = tmp_path / "nonexistent.png"
    with pytest.raises(FileNotFoundError):
        _crop_watermark(missing_path)


# ── _compose_briefs ─────────────────────────────────────────────────────

_LOCKED_STYLE = _INF_STYLES_BY_ID["flat-navy"]
_LOCKED_PALETTE = next(p for p in _INF_PALETTES if p["id"] == "abyss-navy")


@pytest.mark.anyio
async def test_compose_briefs_llm_good_json():
    good_json = json.dumps([
        {"style": "flat-navy", "brief": "Brief 1 art line and data"},
        {"style": "flat-navy", "brief": "Brief 2 art line and data"},
    ])
    with patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.return_value = good_json
        briefs = await _compose_briefs(
            "Full script", ["Block 1 with 10", "Block 2 with 20"],
            _LOCKED_STYLE, _LOCKED_PALETTE)
        assert len(briefs) == 2
        assert briefs[0]["style"] == "flat-navy"
        assert briefs[0]["brief"] == "Brief 1 art line and data"
        assert briefs[1]["style"] == "flat-navy"
        assert briefs[1]["brief"] == "Brief 2 art line and data"
        # the locked palette reaches the LLM prompt
        assert "PALETTE (LOCKED):" in mock_zai.call_args.kwargs["system"]


@pytest.mark.anyio
async def test_compose_briefs_llm_failure_falls_back_to_locked_style():
    locked = _INF_STYLES_BY_ID["swiss"]
    pal = next(p for p in _INF_PALETTES if p["tone"] == "light")
    with patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.side_effect = RuntimeError("network error")
        briefs = await _compose_briefs(
            "Full script", ["Block 1 with 100 tourists", "Block 2 with 50%"], locked, pal
        )
        assert len(briefs) == 2
        # BOTH blocks use the locked style + palette — one visual family
        assert all(b["style"] == "swiss" for b in briefs)
        assert "100 tourists" in briefs[0]["brief"]
        assert "50%" in briefs[1]["brief"]
        assert locked["art"] in briefs[0]["brief"]
        assert pal["accent"] in briefs[0]["brief"]


@pytest.mark.anyio
async def test_compose_briefs_style_drift_falls_back_whole_set():
    """LLM returning any style other than the locked one discards the whole
    LLM pass — partial adoption would break the same-family look."""
    drift = json.dumps([
        {"style": "flat-navy", "brief": "Good brief"},
        {"style": "kawaii", "brief": "Drifted brief"},
    ])
    with patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.return_value = drift
        briefs = await _compose_briefs(
            "Full script", ["Block 1 100", "Block 2 200"],
            _LOCKED_STYLE, _LOCKED_PALETTE)
        assert all(b["style"] == "flat-navy" for b in briefs)
        assert all("flat vector motion-graphics" in b["brief"] for b in briefs)
        assert all(_LOCKED_PALETTE["accent"] in b["brief"] for b in briefs)


@pytest.mark.anyio
async def test_compose_briefs_unknown_style_in_llm_output_falls_back():
    bad_json = json.dumps([
        {"style": "neon-future", "brief": "Unknown style brief"},
    ])
    with patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.return_value = bad_json
        briefs = await _compose_briefs(
            "Full script", ["Block 1 with 100"], _LOCKED_STYLE, _LOCKED_PALETTE)
        assert len(briefs) == 1
        assert briefs[0]["style"] == "flat-navy"
        assert _LOCKED_STYLE["art"] in briefs[0]["brief"]  # template brief, locked look


# ── mood classification + per-article style pick ────────────────────────

# notebooklm 0.8.1 `generate infographic --help` --style choices. Guards the
# whole catalog against CLI drift — swap here when the CLI upgrades.
_CLI_INF_STYLES = {"auto", "sketch-note", "professional", "bento-grid",
                   "editorial", "instructional", "bricks", "clay", "anime",
                   "kawaii", "scientific"}


def test_inf_styles_presets_valid_and_unique():
    assert len(_INF_STYLES) == 16
    assert len({s["id"] for s in _INF_STYLES}) == 16
    assert all(s["preset"] in _CLI_INF_STYLES for s in _INF_STYLES)


def test_art_lines_specify_background_treatment():
    """Naz refine #2 (2026-09-01): no plain solid backgrounds — every style's
    art line must demand a gradient or geometric-abstract treatment."""
    for s in _INF_STYLES:
        assert "gradient" in s["art"] or "geometric" in s["art"], s["id"]


def test_art_lines_own_no_colors():
    """Naz refine #3: the PALETTE layer owns all colors — art lines carry
    treatment only, so style × palette rotate without contradiction."""
    for s in _INF_STYLES:
        assert "#" not in s["art"], f"{s['id']} hardcodes a color"


def test_style_and_palette_pools_sized():
    """"10-20 or more for each" — styles AND palettes both in range."""
    assert 10 <= len(_INF_STYLES) <= 20 or len(_INF_STYLES) > 20
    assert len(_INF_STYLES) >= 10 and len(_INF_PALETTES) >= 10
    assert len({s["id"] for s in _INF_STYLES}) == len(_INF_STYLES)
    assert len({p["id"] for p in _INF_PALETTES}) == len(_INF_PALETTES)
    assert all(s["bg_tone"] in ("dark", "light") for s in _INF_STYLES)
    assert all(p["tone"] in ("dark", "light") for p in _INF_PALETTES)


def test_every_mood_has_style_and_palette_variety():
    tags = {m for s in _INF_STYLES for m in s["moods"]}
    for tag in tags:
        eligible = [s for s in _INF_STYLES if tag in s["moods"]]
        assert len(eligible) >= 3, f"mood {tag} has only {len(eligible)} styles"
    general = [s for s in _INF_STYLES if "general" in s["moods"]]
    assert len(general) >= 3
    ptags = {m for p in _INF_PALETTES for m in p["moods"]}
    for tag in ptags:
        eligible = [p for p in _INF_PALETTES if tag in p["moods"]]
        assert len(eligible) >= 3, f"mood {tag} has only {len(eligible)} palettes"


def test_every_style_tone_has_palettes():
    """Contrast safety: any drawn style must find a palette of its tone."""
    for tone in ("dark", "light"):
        pool = [p for p in _INF_PALETTES if p["tone"] == tone]
        assert len(pool) >= 5, f"tone {tone} starved: {len(pool)}"


def test_spec_demands_isometric_hero_and_non_solid_bg():
    assert "NEVER a plain solid fill" in _TV_SAFE_SPEC
    assert "3D isometric object, scene, or miniature diorama" in _TV_SAFE_SPEC
    # loop-safety survives the new background: particles/shimmer stay banned
    assert "no baked-in particles" in _TV_SAFE_SPEC


def test_classify_moods_buckets():
    assert "hard-news" in _classify_moods(
        "Flooding in the south killed three and forced evacuations.")
    assert "business" in _classify_moods(
        "Exports rose and the baht strengthened as tourism revenue climbed.")
    assert "sport" in _classify_moods(
        "The championship final saw the athlete secure the medal.")
    assert "celebration" in _classify_moods(
        "Bangkok celebrates Songkran with a record-breaking festival.")
    assert _classify_moods(
        "The committee will review the schedule next week.") == []


def test_classify_moods_word_boundaries():
    # 'deadline' must NOT trip 'dead'; 'signature' must NOT trip 'sign'... etc.
    assert "hard-news" not in _classify_moods("They met the deadline for the application.")


def test_pick_inf_look_forced_style_palette_still_rotates():
    s, p = pick_inf_look("Flooding killed three in the south.", forced="kawaii")
    assert s["id"] == "kawaii"
    assert s["pick_source"] == "forced"
    assert p["tone"] == s["bg_tone"]  # contrast safety holds under force


def test_pick_inf_look_mood_matched_both_layers():
    text = "Exports rose, the baht strengthened and GDP grew on tourism revenue."
    for _ in range(30):  # every draw lands inside the business pools, tone-safe
        s, p = pick_inf_look(text)
        assert "business" in s["moods"]
        assert "business" in p["moods"]
        assert p["tone"] == s["bg_tone"]
        assert s["pick_source"] == "mood"


def test_pick_inf_look_neutral_general_pool():
    text = "The committee will review the schedule next week."
    for _ in range(30):
        s, p = pick_inf_look(text)
        assert "general" in s["moods"]
        assert p["tone"] == s["bg_tone"]
        assert s["pick_source"] == "general"


def test_pick_inf_look_unknown_forced_id_falls_through_to_mood():
    s, p = pick_inf_look("Flooding killed three in the south.", forced="nonexistent")
    assert "hard-news" in s["moods"]
    assert p["tone"] == s["bg_tone"]
    assert s["pick_source"] == "mood"


def test_pick_inf_look_rotation_reaches_both_layers():
    """Over many draws on one article, BOTH the style pool and the palette
    pool rotate — style and scheme vary independently."""
    text = "Exports rose, the baht strengthened and GDP grew on tourism revenue."
    styles, palettes = set(), set()
    for _ in range(60):
        s, p = pick_inf_look(text)
        styles.add(s["id"])
        palettes.add(p["id"])
    assert len(styles) >= 3, f"style rotation too narrow: {styles}"
    assert len(palettes) >= 3, f"palette rotation too narrow: {palettes}"


# ── generate_infographics ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_generate_infographics_success(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Deterministic style draw: tourism script → business pool → flat-navy (first)
    import app.newsroom as newsroom_mod
    monkeypatch.setattr(newsroom_mod.random, "choice", lambda seq: seq[0])

    script_text = (
        "EVE2026083101\n"
        "Anchor lede.\n\n"
        "[inf]\n"
        "Thailand welcomed 15 million tourists in 2026.\n"
        "[inf/]\n\n"
        "Closing remarks."
    )

    seen_runs: list[list[str]] = []
    seen_scripts: list[tuple[list[str], bytes | None]] = []

    async def fake_run(argv, timeout=90, env=None, stdin=None):
        seen_runs.append(list(argv))
        if "delete" in argv:
            return 0, b"{}", b""
        if "source" in argv and "add" in argv:
            return 0, b'{"status":"ok"}', b""
        if "download" in argv:
            dest_path = Path(argv[-1])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.new("RGB", (2752, 1536), color=(255, 255, 255))
            img.save(dest_path)
            return 0, b"Infographic saved to: " + str(dest_path).encode(), b""
        return 0, b"{}", b""

    async def fake_script(argv, timeout=90, env=None, stdin=None):
        seen_scripts.append((list(argv), stdin))
        if "create" in argv:
            return {"notebook": {"id": "nid-test-123"}}
        if "generate" in argv:
            return {"task_id": "t1", "status": "completed"}
        return {}

    with patch("app.newsroom._run", side_effect=fake_run), \
         patch("app.newsroom._script", side_effect=fake_script), \
         patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.return_value = json.dumps([
            {"style": "flat-navy", "brief": "Custom brief with 15 million"}
        ])

        res = await generate_infographics(script_text, "auto")

        assert res["slug"] == "EVE2026083101"
        assert res["blocks"] == 1
        assert res["style"]["id"] == "flat-navy"
        assert res["style"]["preset"] == "professional"
        assert "business" in res["style"]["matched_moods"]
        assert res["style"]["pick_source"] == "mood"
        assert res["palette"]["id"] == "abyss-navy"  # first dark mood-match
        assert res["palette"]["tone"] == "dark"  # flat-navy is dark-tone
        assert res["palette"]["accent"] == "#56B4E9"
        assert len(res["files"]) == 1
        assert res["notebook_deleted"] is True
        assert res["errors"] == []

        # Verify generate argv
        gen_call = next((argv, stdin) for argv, stdin in seen_scripts if "generate" in argv)
        gen_argv, gen_stdin = gen_call
        assert "--orientation" in gen_argv
        assert "landscape" in gen_argv
        assert "--detail" in gen_argv
        assert "standard" in gen_argv
        assert "--style" in gen_argv
        assert "professional" in gen_argv
        assert "--prompt-file" in gen_argv
        assert "-" in gen_argv

        # Verify download argv
        dl_argv = next(argv for argv in seen_runs if "download" in argv)
        assert "--latest" in dl_argv
        assert "--force" in dl_argv
        assert str(res["files"][0]["png"]) in dl_argv

        # Verify loop prompt content
        loop_path = Path(res["files"][0]["loop_prompt"])
        assert loop_path.exists()
        loop_text = loop_path.read_text(encoding="utf-8")
        assert "Frames-to-Video" in loop_text
        assert "first and the last frame" in loop_text

        # Verify watermark cropped
        png_path = Path(res["files"][0]["png"])
        with Image.open(png_path) as cropped:
            assert cropped.size == (2752, 1508)


@pytest.mark.anyio
async def test_generate_infographics_block_failure_partial_success(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    script_text = (
        "EVE2026083102\n"
        "[inf]\nBlock 1 with 100\n[inf/]\n"
        "[inf]\nBlock 2 with 200\n[inf/]"
    )

    generate_count = 0

    async def fake_run(argv, timeout=90, env=None, stdin=None):
        if "delete" in argv:
            return 0, b"{}", b""
        if "source" in argv:
            return 0, b'{"status":"ok"}', b""
        if "download" in argv:
            dest_path = Path(argv[-1])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            img = Image.new("RGB", (2752, 1536), color=(255, 255, 255))
            img.save(dest_path)
            return 0, b"Infographic saved", b""
        return 0, b"{}", b""

    async def fake_script(argv, timeout=90, env=None, stdin=None):
        nonlocal generate_count
        if "create" in argv:
            return {"id": "nid-test-456"}
        if "generate" in argv:
            generate_count += 1
            if generate_count == 1:
                raise HTTPException(502, "generate timed out")
            return {"task_id": "t2", "status": "completed"}
        return {}

    with patch("app.newsroom._run", side_effect=fake_run), \
         patch("app.newsroom._script", side_effect=fake_script), \
         patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.return_value = json.dumps([
            {"style": "flat-navy", "brief": "Brief 1"},
            {"style": "editorial-print", "brief": "Brief 2"},
        ])

        res = await generate_infographics(script_text, "auto")

        assert res["blocks"] == 2
        assert len(res["files"]) == 1  # only block 2 succeeded
        assert len(res["errors"]) == 1
        assert "block 1 generate failed" in res["errors"][0]


@pytest.mark.anyio
async def test_generate_infographics_no_blocks_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        await generate_infographics("Script text without infographic markers.")
    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_generate_infographics_forced_style_overrides_mood(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script_text = "EVE2026083103\n[inf]\nFlooding killed three, 500 evacuated.\n[inf/]"

    async def fake_run(argv, timeout=90, env=None, stdin=None):
        if "delete" in argv:
            return 0, b"{}", b""
        if "download" in argv:
            dest_path = Path(argv[-1])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2752, 1536), color=(255, 255, 255)).save(dest_path)
            return 0, b"saved", b""
        return 0, b"{}", b""

    async def fake_script(argv, timeout=90, env=None, stdin=None):
        if "create" in argv:
            return {"notebook": {"id": "nid-f"}}
        return {"task_id": "t", "status": "completed"}

    with patch("app.newsroom._run", side_effect=fake_run), \
         patch("app.newsroom._script", side_effect=fake_script), \
         patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.return_value = ""  # forces template fallback in the locked look
        res = await generate_infographics(script_text, "kawaii")

    assert res["style"]["id"] == "kawaii"
    assert res["style"]["pick_source"] == "forced"
    assert res["palette"]["tone"] == "light"  # kawaii is light-tone
    assert res["errors"] == []


@pytest.mark.anyio
async def test_generate_infographics_same_style_across_blocks(tmp_path, monkeypatch):
    """Whatever the random draw picks, EVERY block's generate argv carries the
    same --style preset — the article reads as one visual family."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script_text = (
        "EVE2026083104\n"
        "[inf]\nBlock one with 100 baht revenue.\n[inf/]\n"
        "[inf]\nBlock two with 200 visitors.\n[inf/]"
    )

    async def fake_run(argv, timeout=90, env=None, stdin=None):
        if "delete" in argv:
            return 0, b"{}", b""
        if "download" in argv:
            dest_path = Path(argv[-1])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2752, 1536), color=(255, 255, 255)).save(dest_path)
            return 0, b"saved", b""
        return 0, b"{}", b""

    async def fake_script(argv, timeout=90, env=None, stdin=None):
        if "create" in argv:
            return {"notebook": {"id": "nid-l"}}
        return {"task_id": "t", "status": "completed"}

    async def zai_echoes_lock(user_prompt, system=None, **kw):
        locked = system.split("LOCKED to '")[1].split("'")[0]
        accent = system.split("accent ")[1].split(",")[0]  # from the PALETTE line
        return json.dumps([
            {"style": locked, "brief": f"{locked} brief {i} accent {accent}"}
            for i in (1, 2)
        ])

    gen_stdins: list[bytes] = []
    gen_argv_styles: list[str] = []

    async def script_spy(argv, timeout=90, env=None, stdin=None):
        if "generate" in argv:
            gen_argv_styles.append(argv[argv.index("--style") + 1])
            gen_stdins.append(stdin)
        return await fake_script(argv, timeout=timeout, env=env, stdin=stdin)

    with patch("app.newsroom._run", side_effect=fake_run), \
         patch("app.newsroom._script", side_effect=script_spy), \
         patch("app.newsroom.zai.zai_message", side_effect=zai_echoes_lock):
        res = await generate_infographics(script_text, "auto")

    assert len(gen_argv_styles) == 2
    assert len(set(gen_argv_styles)) == 1  # SAME preset for both blocks
    assert gen_argv_styles[0] == res["style"]["preset"]
    assert len(gen_stdins) == 2 and all(gen_stdins)
    # SAME palette hex in both briefs — the color scheme locks with the style
    assert res["palette"]["accent"].encode() in gen_stdins[0]
    assert res["palette"]["accent"].encode() in gen_stdins[1]


# ── Routes & Confinement ────────────────────────────────────────────────


def test_api_newsroom_infographic_generate_route():
    with patch("app.newsroom.generate_infographics", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = {
            "slug": "EVE123",
            "dir": "/tmp/test",
            "blocks": 1,
            "files": [],
            "notebook_deleted": True,
            "errors": [],
            "warnings": [],
        }
        client = TestClient(app)
        resp = client.post(
            "/api/newsroom/infographic/generate",
            json={"text": "[inf]\nTest\n[inf/]", "style": "auto"},
        )
        assert resp.status_code == 200
        assert resp.json()["slug"] == "EVE123"


def test_api_newsroom_infographic_file_confinement(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out_base = tmp_path / "Downloads" / "Newsroom Infographics" / "EVE123"
    out_base.mkdir(parents=True, exist_ok=True)
    test_png = out_base / "EVE123-inf1.png"
    Image.new("RGB", (100, 100), color=(255, 255, 255)).save(test_png)

    client = TestClient(app)

    # 1. Path traversal escape -> 403
    r = client.get("/api/newsroom/infographic/file?f=../../etc/passwd")
    assert r.status_code == 403

    # 2. Non-png/non-txt extension -> 403
    r = client.get("/api/newsroom/infographic/file?f=EVE123/evil.py")
    assert r.status_code == 403

    # 3. Missing file -> 404
    r = client.get("/api/newsroom/infographic/file?f=EVE123/missing.png")
    assert r.status_code == 404

    # 4. Valid file -> 200 FileResponse
    r = client.get("/api/newsroom/infographic/file?f=EVE123/EVE123-inf1.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


@pytest.mark.anyio
async def test_generate_infographics_never_raises_past_step1(tmp_path, monkeypatch):
    """Validator regression (2026-08-31): CLI create 502 / source-add timeout must
    land in errors — the advisory pipeline never raises past step 1."""
    from unittest.mock import AsyncMock, patch
    from app.newsroom import generate_infographics

    script = "EVE123\n\n[inf]\nThailand saw 15 million visitors.\n[inf/]\n"
    # (Somatic's copy also monkeypatches app.newsroom.HOME — home's newsroom
    # module has no HOME constant; Path.home() is already tmp-scoped above.)

    # create explodes (auth-broken CLI, the realistic trigger)
    async def boom_script(argv, timeout=90, env=None, stdin=None):
        raise HTTPException(502, "notebooklm create exploded")
    with patch("app.newsroom._run", new_callable=AsyncMock), \
         patch("app.newsroom._script", side_effect=boom_script), \
         patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mz:
        mz.return_value = "[]"
        res = await generate_infographics(script, "auto")
    assert res["files"] == [] and res["errors"], "create failure must land in errors"

    # source-add times out (_run raises 504) — notebook still deleted, no raise
    calls = {"delete": 0}
    async def timeout_run(argv, timeout=90, env=None, stdin=None):
        if "create" in argv:
            return 0, b'{"notebook": {"id": "nid-x"}}', b""
        if "delete" in argv:
            calls["delete"] += 1
            return 0, b"deleted", b""
        raise HTTPException(504, "source add timed out")
    async def ok_script(argv, timeout=90, env=None, stdin=None):
        if "create" in argv:
            return {"notebook": {"id": "nid-x"}}
        return {"task_id": "t", "status": "completed"}
    with patch("app.newsroom._run", side_effect=timeout_run), \
         patch("app.newsroom._script", side_effect=ok_script), \
         patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mz:
        mz.return_value = "[]"
        res = await generate_infographics(script, "auto")
    assert any("source" in e.lower() or "timed out" in e.lower() for e in res["errors"])
    assert calls["delete"] == 1


# ── motion catalog: pick_inf_motion + loop prompt ─────────────────────
def test_motion_catalog_size_and_shape():
    from app.newsroom import _INF_MOTIONS
    assert 20 <= len(_INF_MOTIONS) <= 40
    ids = [m["id"] for m in _INF_MOTIONS]
    assert len(ids) == len(set(ids))
    for m in _INF_MOTIONS:
        assert m["label"] and m["tail"] and m["kinds"] and m["moods"]
        assert set(m["kinds"]) <= {"stat", "process", "map", "photo", "hero", "type"}
        assert "general" in m["moods"] or len(m["moods"]) >= 1


def test_motion_tails_are_camera_safe():
    """Tails direct INTERNAL elements only — camera language lives in the
    skeleton, never in a tail (loop safety: one voice per instruction)."""
    from app.newsroom import _INF_MOTIONS
    forbidden = ("zoom", "camera", "pan ", "tilt", "fade to black", "cut to")
    for m in _INF_MOTIONS:
        low = m["tail"].lower()
        for token in forbidden:
            assert token not in low, f"{m['id']} tail contains '{token}'"


def test_classify_block_kinds_routing():
    from app.newsroom import _classify_block_kinds
    assert "stat" in _classify_block_kinds("Revenue rose 12 percent to 1.2 trillion baht.")
    assert "process" in _classify_block_kinds("First apply online. Then wait five days. Finally collect the card.")
    assert "map" in _classify_block_kinds("Flooding hit provinces in the north along the river.")
    assert "photo" in _classify_block_kinds("Footage showed the moment of the crash.")
    assert "type" in _classify_block_kinds("\u201cA historic day,\u201d the PM said.")
    assert _classify_block_kinds("The ministry announced a new initiative.") == []


def test_pick_inf_motion_forced_and_fallbacks():
    from app.newsroom import pick_inf_motion
    plain = "The ministry announced a new initiative."
    forced = pick_inf_motion(plain, [], "flag-wave")
    assert forced["id"] == "flag-wave" and forced["pick_source"] == "forced"
    unknown = pick_inf_motion(plain, [], "no-such-motion")
    assert unknown["pick_source"] in {"mood", "kind", "general"}
    fallen = pick_inf_motion(plain, [])
    assert fallen["pick_source"] == "general"  # no kinds → hero/general pool


def test_pick_inf_motion_kind_never_leaks():
    """A stat block's motion always comes from a kind-matched pool — never a
    typography-only or photo-only motion."""
    from app.newsroom import _INF_MOTIONS_BY_ID, pick_inf_motion
    stat_block = "Tourism revenue rose 12 percent to 1.2 trillion baht in Q3."
    for _ in range(20):
        rec = pick_inf_motion(stat_block, ["business"])
        assert "stat" in rec["kinds"], f"leaked to {rec['id']} via {rec['pick_source']}"
        assert _INF_MOTIONS_BY_ID[rec["id"]] is not None


def test_pick_inf_motion_rotation_breadth():
    from app.newsroom import pick_inf_motion
    stat_block = "Exports grew 5 percent to 500 billion baht."
    seen = {pick_inf_motion(stat_block, ["business", "hard-news", "tech"])[0 if False else "id"]
            for _ in range(60)}
    assert len(seen) >= 5  # the pool actually rotates


def test_loop_prompt_contains_contract():
    from app.newsroom import _loop_prompt, pick_inf_motion
    motion = pick_inf_motion("Revenue rose 12 percent.", ["business"])
    txt = _loop_prompt("EVE2026090201 story", "12 percent, 1.2 trillion baht", motion)
    for needle in (
        "first and", "last frame",          # first=last frame contract
        "PERFECTLY STATIC TRIPOD",           # camera lock
        motion["tail"],                      # per-block motion
        "12 percent",                        # figures verbatim
        "ffmpeg", "xfade",                   # post crossfade fix
        "reverse", "0.458333",               # de-jerk: reverse-bridge, exact completion
        "720p", "Omni Flash",                # engine guidance
    ):
        assert needle in txt, f"loop txt missing: {needle}"


@pytest.mark.anyio
async def test_generate_infographics_motion_forced_writes_txt(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    script_text = "EVE2026090205\n[inf]\nTourism revenue rose 12 percent to 1.2 trillion baht.\n[inf/]"

    async def fake_run(argv, timeout=90, env=None, stdin=None):
        if "delete" in argv:
            return 0, b"{}", b""
        if "download" in argv:
            dest_path = Path(argv[-1])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2752, 1536), color=(255, 255, 255)).save(dest_path)
            return 0, b"saved", b""
        return 0, b"{}", b""

    async def fake_script(argv, timeout=90, env=None, stdin=None):
        if "create" in argv:
            return {"notebook": {"id": "nid-m"}}
        return {"task_id": "t", "status": "completed"}

    with patch("app.newsroom._run", side_effect=fake_run), \
         patch("app.newsroom._script", side_effect=fake_script), \
         patch("app.newsroom.zai.zai_message", new_callable=AsyncMock) as mock_zai:
        mock_zai.return_value = ""
        res = await generate_infographics(script_text, "auto", "flag-wave")

    assert res["errors"] == []
    f = res["files"][0]
    assert f["motion"]["id"] == "flag-wave"
    assert f["motion"]["pick_source"] == "forced"
    loop_txt = Path(f["loop_prompt"]).read_text(encoding="utf-8")
    assert "Flag wave" in loop_txt
    assert "flags and cloth elements wave" in loop_txt
    assert "ffmpeg" in loop_txt
