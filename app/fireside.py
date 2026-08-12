"""The Fireside — video annotation and production cue generation router.

Accepts episode scripts for NBT World's "The Fireside" and returns typed
production cues (chapter, onscreen, broll, note) for video editors.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from .zai import zai_message

try:
    from .thailandnow import _parse_json_lenient
except Exception:
    def _parse_json_lenient(text: str):
        """Parse JSON from an LLM string, tolerating ```json fences and surrounding prose.
        Returns the parsed object (usually a list) or None."""
        if not text:
            return None
        s = text.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s).strip()
            s = re.sub(r"\n?```$", "", s).strip()
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("[", "]"), ("{", "}")):
            i, j = s.find(opener), s.rfind(closer)
            if i != -1 and j > i:
                try:
                    return json.loads(s[i:j + 1])
                except json.JSONDecodeError:
                    continue
        return None

router = APIRouter()


class ProposeRequest(BaseModel):
    script: str = ""
    title: str | None = None


@router.post("/api/fireside/propose")
async def propose(payload: ProposeRequest = Body(...)) -> dict:
    """Propose chronological production cues from an episode script."""
    if not payload.script or not payload.script.strip():
        raise HTTPException(status_code=400, detail="script is required")

    try:
        gem_path = Path(__file__).parent / "gems" / "fireside-annotate.md"
        system = gem_path.read_text(encoding="utf-8")
        user = f"Title: {payload.title.strip()}\n\nScript:\n{payload.script.strip()}" if (payload.title and payload.title.strip()) else payload.script.strip()
        raw = await zai_message(user, max_tokens=8192, system=system, model="glm-5", timeout=180)
        parsed = _parse_json_lenient(raw)

        if isinstance(parsed, list):
            return {"cues": parsed, "mode": "direct"}
        if isinstance(parsed, dict) and isinstance(parsed.get("cues"), list):
            return {"cues": parsed["cues"], "mode": "direct"}
        return {"cues": [], "mode": "degraded"}
    except HTTPException as e:
        if e.status_code == 400:
            raise
        return {"cues": [], "mode": "degraded"}
    except Exception:
        return {"cues": [], "mode": "degraded"}


if __name__ == "__main__":
    fake_raw = '```json\n[{"type": "chapter", "text": "THE NORTH / ภาคเหนือ", "beat": "Intro"}]\n```'
    cues = _parse_json_lenient(fake_raw)
    assert isinstance(cues, list) and len(cues) > 0, f"Expected non-empty list, got {cues}"
    assert cues[0]["type"] == "chapter"
    print("Lenient parse self-test passed successfully.")
