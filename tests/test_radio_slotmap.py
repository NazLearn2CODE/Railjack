"""Regression guard for the RADIO fill slotmap (bug 2026-08-19).

The 2026-08-18 slice decommission deleted the separate slice path but forgot
to fold ("AM",4) into the weekday-global slotmap — APPLY placed only 10 of
11 pieces and AM GLOBAL slot 4 stayed empty (Naz, live). Guards the map shape
and the full-assignment behavior against the canonical skill-library script.
"""
import importlib.util
from pathlib import Path

_RNEWS = Path.home() / "skill-library" / "skills" / "newsroom" / "scripts" / "radio_news.py"

spec = importlib.util.spec_from_file_location("radio_news", _RNEWS)
rn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rn)


def _piece(n: int, region: str = "") -> dict:
    return {"title": f"p{n}", "url": f"https://x/{n}", "region": region}


def test_weekday_global_slotmap_has_11_slots_incl_am4():
    m = rn.build_slotmap("global", "weekday")
    assert len(m) == 11  # 10+1 → 11 quota (slice fold, 2026-08-18)
    assert ("AM", 4) in m  # the fold target restored 2026-08-19
    assert m[:4] == [("AM", 1), ("AM", 2), ("AM", 3), ("AM", 4)]


def test_other_slotmaps_unchanged():
    assert len(rn.build_slotmap("global", "weekend")) == 7
    assert len(rn.build_slotmap("business", "weekday")) == 10
    assert len(rn.build_slotmap("business", "weekend")) == 6


def test_assign_pieces_fills_all_11_weekday_global():
    slotmap = rn.build_slotmap("global", "weekday")
    pieces = [_piece(1, "SEA"), _piece(2, "SEA"), _piece(3, "SEA")] + [
        _piece(i) for i in range(4, 12)
    ]  # 3 SEA + 8 rest = 11
    assignment = rn.assign_pieces(slotmap, pieces, "global")
    assert len(assignment) == 11  # nobody dropped
    # SEA leads every broadcast (slot 1 of each tab)
    assert assignment[("AM", 1)]["region"] == "SEA"
    assert assignment[("MIDDAY", 1)]["region"] == "SEA"
    assert assignment[("EVE", 1)]["region"] == "SEA"
    # AM4 (the restored slot) gets a piece
    assert assignment[("AM", 4)] is not None
