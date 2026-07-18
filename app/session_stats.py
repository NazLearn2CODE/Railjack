"""GET /api/session — current Claude session telemetry, read from disk.

Newest ``~/.claude/projects/<slug>/<uuid>.jsonl`` by mtime (``idle`` if >10 min
stale). Context tokens = ``input_tokens + cache_read_input_tokens +
cache_creation_input_tokens`` of the last assistant ``message.usage``. The
session's ``message.model`` → provider via the ``providers`` config (regex,
``window_hours``, ``context_limit``); unknown model → provider ``"?"`` with
default 5 h / 200 k, ctx % still reported.

5 h block math (ccusage convention): a new block starts at the first event
after a ≥ window gap from the previous event OR past the previous block's
``reset_at``; block start is floored to the hour; ``reset_at = block_start +
window``. We return the block holding the newest event. 30 s in-process cache.

Only files matching the active provider's model regex contribute block events
(the active session + its siblings); pruned to mtime within ``window + 1 h``.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter

from .config import CONFIG

router = APIRouter()

SESSIONS_ROOT = Path.home() / ".claude" / "projects"

_CACHE_TTL = 30
_DEFAULT_WINDOW = 5
_DEFAULT_LIMIT = 200_000
_IDLE_MIN = 10
_cache: tuple[float, dict] | None = None


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _match_provider(model: str | None, providers) -> tuple[str, re.Pattern | None, float, int]:
    """Return (name, compiled regex | None, window_hours, context_limit)."""
    if model:
        for p in providers:
            rx = re.compile(p.model_match)
            if rx.search(model):
                return p.name, rx, p.window_hours, p.context_limit
    return "?", None, _DEFAULT_WINDOW, _DEFAULT_LIMIT


def _scan(path: Path) -> tuple[list[datetime], str | None, int]:
    """One pass over a transcript: (timestamps, last assistant model, last ctx tokens).

    ponytail: single full scan per file gives block timestamps AND the trailing
    usage line in one read — simpler than the tail-then-fallback dance, and the
    mtime cutoff bounds it to recent files. If a transcript balloons past
    ~100 MB and scan latency shows, gate usage behind a tail read (last 256 KB)
    while keeping the full scan only for block math.
    """
    times: list[datetime] = []
    model: str | None = None
    ctx = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts:
                    times.append(ts)
                if obj.get("type") == "assistant":
                    msg = obj.get("message") or {}
                    m = msg.get("model")
                    if m:
                        model = m
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        ctx = (
                            int(usage.get("input_tokens", 0))
                            + int(usage.get("cache_read_input_tokens", 0))
                            + int(usage.get("cache_creation_input_tokens", 0))
                        )
    except OSError:
        pass
    return times, model, ctx


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _current_block(
    events: list[datetime], window_hours: float
) -> tuple[datetime | None, datetime | None]:
    """The block holding the newest event: (block_start, reset_at)."""
    if not events:
        return None, None
    window = timedelta(hours=window_hours)
    ev = sorted(events)
    block_start = _floor_hour(ev[0])
    reset_at = block_start + window
    prev = ev[0]
    for t in ev[1:]:
        if t >= reset_at or (t - prev) >= window:
            block_start = _floor_hour(t)
            reset_at = block_start + window
        prev = t
    return block_start, reset_at


def _all_jsonl() -> list[Path]:
    try:
        return list(SESSIONS_ROOT.rglob("*.jsonl"))
    except OSError:
        return []


def _empty() -> dict:
    return {
        "provider": "?",
        "model": "",
        "context_tokens": 0,
        "context_limit": _DEFAULT_LIMIT,
        "context_pct": 0,
        "reset_at": None,
        "idle": True,
    }


def _session_state() -> dict:
    files = _all_jsonl()
    if not files:
        return _empty()

    now = datetime.now(timezone.utc)
    # newest by mtime (resolve ties deterministically by path)
    newest = max(files, key=lambda p: (p.stat().st_mtime, str(p)))
    try:
        nstat = newest.stat()
    except OSError:
        return _empty()
    newest_mtime = datetime.fromtimestamp(nstat.st_mtime, tz=timezone.utc)
    idle = (now - newest_mtime) > timedelta(minutes=_IDLE_MIN)

    n_times, model, ctx = _scan(newest)
    name, rx, window_h, limit = _match_provider(model, CONFIG.providers)

    # Block math: active session's events + sibling files whose model matches
    # the provider regex, pruned to mtime within window+1 h.
    events = list(n_times)
    cutoff = now - timedelta(hours=window_h + 1)
    for f in files:
        if f == newest:
            continue
        try:
            mt = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mt < cutoff:
            continue
        # ponytail: unknown provider (rx is None) → no sibling matches; the
        # active session's own events still define the block.
        if rx is None:
            continue
        ftimes, fmodel, _ = _scan(f)
        if fmodel and rx.search(fmodel):
            events.extend(ftimes)

    block_start, reset_at = _current_block(events, window_h)
    pct = round(ctx / limit * 100) if limit else 0
    return {
        "provider": name,
        "model": model or "",
        "context_tokens": ctx,
        "context_limit": limit,
        "context_pct": pct,
        "reset_at": reset_at.isoformat() if reset_at else None,
        "idle": idle,
    }


@router.get("/api/session")
def session() -> dict:
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    data = _session_state()
    _cache = (now, data)
    return data
