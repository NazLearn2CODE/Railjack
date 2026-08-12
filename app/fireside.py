"""The Fireside — video annotation and production cue generation router.

Accepts episode scripts for NBT World's "The Fireside" and returns typed
production cues (chapter, onscreen, broll, note) for video editors.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

try:
    from .zai import zai_message
except (ImportError, ValueError):
    try:
        from zai import zai_message
    except (ImportError, ValueError):
        zai_message = None

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


# ponytail: difflib longest-common-run over SUB-tokenized Whisper words. Handles
# the real failure modes (merged tokens like "just $20"; digits-vs-words like
# "$20" vs "twenty dollars"; minor mishears) by matching on the stable content
# words around them. If a real episode still shows poor accuracy (heavy accents,
# very long scripts), upgrade to a full global script<->transcript forced-alignment pass.
_STOP = {"the", "a", "an", "to", "of", "and", "or", "in", "on", "at", "is", "it",
         "for", "that", "this", "with", "as", "by", "be", "are", "was", "were"}


def _align_anchor(transcript_words: list[dict], anchor: str | None) -> float | None:
    """Align an anchor phrase to word timestamps in a transcript.

    Sub-tokenizes each Whisper word (so a merged token like "just $20" still aligns
    token-by-token with the script) and returns the start time of the longest stable
    matching run. None if no confident match.
    """
    if not anchor or not transcript_words:
        return None
    anchor_tokens = re.findall(r"\w+", anchor.lower())
    if not anchor_tokens:
        return None
    trans_tokens: list[str] = []
    trans_starts: list[float] = []
    for w in transcript_words:
        start = w.get("start")
        for t in re.findall(r"\w+", (w.get("word", "") or "").lower()):
            trans_tokens.append(t)
            trans_starts.append(start)
    if not trans_tokens:
        return None
    blocks = difflib.SequenceMatcher(None, trans_tokens, anchor_tokens).get_matching_blocks()
    longest = max(blocks, key=lambda b: b.size)
    if longest.size < 2 or longest.a >= len(trans_tokens):
        return None
    # don't trust a short run that only coincides on stopwords (the/a/at/…)
    if longest.size < 3 and trans_tokens[longest.a] in _STOP:
        return None
    return float(trans_starts[longest.a])


@router.post("/api/fireside/propose")
async def propose(payload: ProposeRequest = Body(...)) -> dict:
    """Propose chronological production cues from an episode script."""
    if not payload.script or not payload.script.strip():
        raise HTTPException(status_code=400, detail="script is required")

    try:
        gem_path = Path(__file__).parent / "gems" / "fireside-annotate.md"
        system = gem_path.read_text(encoding="utf-8")
        user = f"Title: {payload.title.strip()}\n\nScript:\n{payload.script.strip()}" if (payload.title and payload.title.strip()) else payload.script.strip()
        if zai_message is None:
            return {"cues": [], "mode": "degraded"}
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


@router.post("/api/fireside/align")
async def align(
    video: UploadFile | None = File(None),
    video_url: str | None = Form(None),
    cues: str = Form(...),
) -> dict:
    """Auto-align cues to spoken episode transcript via Groq Whisper."""
    has_video = video is not None and bool(getattr(video, "filename", None))
    has_url = video_url is not None and bool(video_url.strip())

    if (has_video and has_url) or (not has_video and not has_url):
        raise HTTPException(status_code=400, detail="Exactly one of video or video_url is required")

    try:
        cues_list = json.loads(cues) if isinstance(cues, str) else (cues or [])
        if not isinstance(cues_list, list):
            cues_list = []
    except Exception:
        cues_list = []

    mp4_path: str | None = None
    wav_path: str | None = None

    try:
        # 1. Obtain video bytes & write to temp mp4
        if has_video and video is not None:
            video_bytes = await video.read()
        else:
            assert video_url is not None
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                r = await client.get(video_url.strip())
                r.raise_for_status()
                video_bytes = r.content

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f_mp4:
            f_mp4.write(video_bytes)
            mp4_path = f_mp4.name

        # 2. Extract mono 16kHz audio with ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_wav:
            wav_path = f_wav.name

        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", mp4_path, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", wav_path],
            capture_output=True,
            timeout=600,
        )
        if proc.returncode != 0:
            return {"cues": cues_list, "mode": "degraded", "hint": "ffmpeg audio extraction failed"}

        # 3. Read GROQ_API_KEY from environment
        groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not groq_key:
            out_cues = [{**c, "t": c.get("t", None)} for c in cues_list]
            return {"cues": out_cues, "mode": "degraded", "hint": "set GROQ_API_KEY"}

        # 4. Transcribe with Groq Whisper API (word-level timestamps)
        wav_bytes = Path(wav_path).read_bytes()
        wav_name = Path(wav_path).name
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": (wav_name, wav_bytes, "audio/wav")},
                data={
                    "model": "whisper-large-v3-turbo",
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": "word",
                    "temperature": "0",
                },
            )
            r.raise_for_status()
            resp_data = r.json()
            words = resp_data.get("words", [])

        # 5 & 6. Align each cue using _align_anchor
        matched_count = 0
        filled_cues = []
        for cue in cues_list:
            c = dict(cue)
            anchor = c.get("script_anchor")
            if anchor and isinstance(anchor, str) and anchor.strip():
                t = _align_anchor(words, anchor.strip())
                if t is not None:
                    c["t"] = t
                    matched_count += 1
            filled_cues.append(c)

        return {
            "cues": filled_cues,
            "mode": "aligned",
            "matched": matched_count,
            "total": len(filled_cues),
        }

    except HTTPException:
        raise
    except Exception as e:
        return {"cues": cues_list, "mode": "degraded", "hint": str(e)}
    finally:
        for p in (mp4_path, wav_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


if __name__ == "__main__":
    fake_raw = '```json\n[{"type": "chapter", "text": "THE NORTH / ภาคเหนือ", "beat": "Intro"}]\n```'
    cues = _parse_json_lenient(fake_raw)
    assert isinstance(cues, list) and len(cues) > 0, f"Expected non-empty list, got {cues}"
    assert cues[0]["type"] == "chapter"
    print("Lenient parse self-test passed successfully.")

    # align test
    tx = [{"word": "welcome", "start": 0.0}, {"word": "to", "start": 0.4},
          {"word": "the", "start": 0.6}, {"word": "north", "start": 0.9}]
    val = _align_anchor(tx, "welcome to the north")
    assert val is not None and abs(val - 0.0) < 1e-6
    assert _align_anchor(tx, "nonexistent gibberish xyz") is None
    # real failure mode: Whisper merges "just $20" into one token and writes "$20"
    # not "twenty dollars" — must still land on the stable word "fiber".
    tx2 = [{"word": "Gigabit", "start": 3.24}, {"word": "fiber", "start": 3.86},
           {"word": "starts", "start": 4.28}, {"word": "at", "start": 4.72},
           {"word": "just $20", "start": 4.98}, {"word": "a", "start": 5.66},
           {"word": "month.", "start": 6.16}]
    val2 = _align_anchor(tx2, "fiber starts at just twenty dollars a month")
    assert val2 is not None and abs(val2 - 3.86) < 1e-6, f"expected ~3.86 (fiber), got {val2}"
    print("Fireside self-tests passed.")
