"""GET /api/session — current Claude session telemetry, read from disk.

Newest ``~/.claude/projects/<slug>/<uuid>.jsonl`` by mtime (``idle`` if >10 min
stale). Context tokens = ``input_tokens + cache_read_input_tokens +
cache_creation_input_tokens + output_tokens`` of the last assistant
``message.usage`` (input is the conversation at request time; +output is the
post-turn fill the gauge should show). The denominator is the model's *real*
context limit, resolved per-model from a curated table (``_MODEL_CONTEXT_
LIMITS``) — provider ``context_limit`` is only the fallback for unlisted
models, and an empirical floor (``_max_seen``) clamps it so a stale entry can
never push the gauge past 100 %. The session's ``message.model`` → provider
via the ``providers`` config (regex, ``window_hours``); unknown model →
provider ``"?"`` with default 5 h / 200 k, ctx % still reported.

Session %/reset come from the provider's **official usage API** when its
``usage_source`` adapter is configured (``source: "api"``): anthropic-oauth →
the same endpoint Claude Code's /usage uses; zai-quota → rate-ck's endpoint.
On any API failure we fall back to the 5 h block heuristic below and mark
``source: "estimate"``.

5 h block math (ccusage convention, ESTIMATE ONLY — measured ~80 min drift vs
the real Anthropic window on 2026-07-18): a new block starts at the first
event after a ≥ window gap OR past the previous block's ``reset_at``; block
start is floored to the hour; ``reset_at = block_start + window``. Only files
matching the active provider's model regex contribute events. 30 s cache.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter

from .config import CONFIG

router = APIRouter()

SESSIONS_ROOT = Path.home() / ".claude" / "projects"
# OAuth token maintained (and refreshed) by Claude Code itself — we only read it.
CREDS_FILE = Path.home() / ".claude" / ".credentials.json"
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
_USAGE_TIMEOUT = 5.0

_CACHE_TTL = 30
_DEFAULT_WINDOW = 5
_DEFAULT_LIMIT = 200_000
_IDLE_MIN = 10
# Anthropic's /usage endpoint is tightly rate-limited and 429s intermittently;
# reuse the last good reading this long so the strip doesn't flap to the estimate.
_USAGE_RETAIN = 600
_cache: tuple[float, dict] | None = None
_last_usage: dict[str, tuple[float, dict]] = {}

# Per-model context limits — the real denominator for the CTX %. Public, stable
# specs; z.ai's /models endpoint returns no context_window, so a curated table
# is the source of truth (anthropic /v1/models has it, but a static table avoids
# the extra call/failure mode for values that never change). First regex match
# wins; provider.context_limit is the fallback. Extend as new models ship.
_MODEL_CONTEXT_LIMITS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"(^|/)gemini-"), 1_000_000),  # Gemini 3.x Flash — 1M context window (AI Pro)
    (re.compile(r"(^|/)cco-glm"), 1_000_000),  # cco alias (OpenRouter GLM-5.2) — same 1M context
    (re.compile(r"(^|/)glm-5\.2"), 1_000_000),  # z.ai: 200K → 1M extension
    (re.compile(r"(^|/)claude-haiku"), 200_000),
    (re.compile(r"(^|/)claude-"), 1_000_000),  # current Claude line is 1M except Haiku (matched above)
]
# Largest input context ever observed per model — proof its real limit is ≥ that,
# so clamping a stale table entry to this floor keeps the gauge ≤ 100 % until the
# table catches up. Creeps up for unknown models; no-op when the table is right.
_max_seen: dict[str, int] = {}


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _match_provider(model: str | None, providers):
    """Return (matched Provider | None, compiled regex | None)."""
    if model:
        for p in providers:
            rx = re.compile(p.model_match)
            if rx.search(model):
                return p, rx
    return None, None


def _resolve_context_limit(model: str | None, fallback: int) -> int:
    """Model's real context window: table match, else the provider fallback.

    Re-resolved from the live model string every poll, so a mid-session
    ``/model`` switch picks up the new limit automatically — no switch detection.
    """
    for rx, lim in _MODEL_CONTEXT_LIMITS:
        if model and rx.search(model):
            return lim
    return fallback


# ---------------------------------------------------------------- usage adapters
#
# Official per-provider usage APIs — the calibrated source for session %/reset
# (2026-07-18: the JSONL block heuristic drifted ~80 min vs Claude Code's own
# /usage; these endpoints match it). Each returns
# {"session_pct": int, "weekly_pct": int | None, "reset_at": iso-str} or None
# on any failure (offline, 401, missing key) — caller falls back to heuristic.
# NEVER log or return tokens/keys.


def _usage_anthropic() -> dict | None:
    """Claude Code's own /usage source: OAuth usage endpoint, token from disk."""
    try:
        tok = json.loads(CREDS_FILE.read_text())["claudeAiOauth"]["accessToken"]
        r = httpx.get(
            ANTHROPIC_USAGE_URL,
            headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"},
            timeout=_USAGE_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        five, seven = d.get("five_hour") or {}, d.get("seven_day") or {}
        if five.get("utilization") is None or not five.get("resets_at"):
            return None
        return {
            "session_pct": round(five["utilization"]),
            "weekly_pct": round(seven["utilization"]) if seven.get("utilization") is not None else None,
            "reset_at": five["resets_at"],
        }
    except Exception:
        return None


def _usage_zai(key_env: str | None) -> dict | None:
    """rate-ck's endpoint: TOKENS_LIMIT is the binding coding-plan quota."""
    key = os.environ.get(key_env or "ZAI_API_KEY")
    if not key:
        return None
    try:
        r = httpx.get(
            ZAI_QUOTA_URL, headers={"Authorization": f"Bearer {key}"}, timeout=_USAGE_TIMEOUT
        )
        r.raise_for_status()
        limits = (r.json().get("data") or {}).get("limits") or []
        tk = next((x for x in limits if x.get("type") == "TOKENS_LIMIT"), None)
        if not tk or tk.get("percentage") is None or not tk.get("nextResetTime"):
            return None
        reset = datetime.fromtimestamp(tk["nextResetTime"] / 1000, tz=timezone.utc)
        return {
            "session_pct": round(tk["percentage"]),
            "weekly_pct": None,
            "reset_at": reset.isoformat(),
        }
    except Exception:
        return None


def _usage_cco_spend() -> dict | None:
    # OpenRouter $5/mo per-key cap; authoritative tracker is `cco-usage --cap 5`.
    LOG_PATH = Path.home() / ".claude" / "cco-usage.jsonl"
    CAP = 5.0

    try:
        now = datetime.now(timezone.utc)
        if now.month == 12:
            reset_dt = datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        else:
            reset_dt = datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        reset_at = reset_dt.isoformat()

        if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
            return {"session_pct": 0, "weekly_pct": None, "reset_at": reset_at}

        spent = 0.0
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                ts_str = data.get("ts")
                if not ts_str:
                    continue
                dt = _parse_ts(ts_str)
                if not dt:
                    continue
                dt_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                if dt_utc.year == now.year and dt_utc.month == now.month:
                    spent += float(data.get("cost") or 0.0)

        if spent == 0.0:
            return {"session_pct": 0, "weekly_pct": None, "reset_at": reset_at}

        return {
            "session_pct": round(spent / CAP * 100),
            "weekly_pct": None,
            "reset_at": reset_at,
            "source_note": f"${spent:.4f} / ${CAP:.2f}",
        }
    except Exception:
        return None


def _usage_agy_tokens() -> dict | None:
    """agy (Antigravity CLI / Gemini) 7-day rolling window usage adapter."""
    LOG_PATH = Path.home() / ".claude" / "agy-usage.jsonl"
    try:
        now = datetime.now(timezone.utc)
        window_days = 7
        if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
            reset_at = (now + timedelta(days=window_days)).isoformat()
            return {"session_pct": 0, "weekly_pct": 0, "reset_at": reset_at}

        tot_tokens = 0
        earliest_ts = None
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                ts_str = data.get("ts")
                if not ts_str:
                    continue
                dt = _parse_ts(ts_str)
                if not dt:
                    continue
                dt_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                if (now - dt_utc) <= timedelta(days=window_days):
                    if earliest_ts is None or dt_utc < earliest_ts:
                        earliest_ts = dt_utc
                    tot_tokens += int(data.get("total_tokens") or 0)

        reset_start = earliest_ts or now
        reset_at = (reset_start + timedelta(days=window_days)).isoformat()
        
        EST_WEEKLY_CAP = 10_000_000
        weekly_pct = min(100, round((tot_tokens / EST_WEEKLY_CAP) * 100))

        return {
            "session_pct": weekly_pct,
            "weekly_pct": weekly_pct,
            "reset_at": reset_at,
            "source_note": f"{tot_tokens:,} tokens / 7d",
        }
    except Exception:
        return None


def _scan_agy_log() -> tuple[datetime | None, str | None, int]:
    """Scans ~/.claude/agy-usage.jsonl for the latest agy call."""
    LOG_PATH = Path.home() / ".claude" / "agy-usage.jsonl"
    if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
        return None, None, 0
    try:
        last_line = None
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last_line = line.strip()
        if not last_line:
            return None, None, 0
        data = json.loads(last_line)
        ts = _parse_ts(data.get("ts", ""))
        model = data.get("model")
        ctx = (
            int(data.get("input_tokens", 0) or 0)
            + int(data.get("output_tokens", 0) or 0)
            + int(data.get("thinking_tokens", 0) or 0)
        )
        return ts, model, ctx
    except Exception:
        return None, None, 0


_USAGE_ADAPTERS = {
    "anthropic-oauth": lambda p: _usage_anthropic(),
    "zai-quota": lambda p: _usage_zai(p.key_env),
    "cco-spend": lambda p: _usage_cco_spend(),
    "agy-tokens": lambda p: _usage_agy_tokens(),
}


def _fetch_usage(provider) -> dict | None:
    """Provider's official usage API, with last-good retention.

    A raw failure (e.g. Anthropic's /usage 429ing every other poll) would flap
    the strip to the JSONL estimate. So on failure we reuse the last successful
    reading for up to ``_USAGE_RETAIN`` s: ``reset_at`` is absolute so the
    countdown stays correct, and ``session_pct`` only goes slightly stale — fine
    for a slow 5 h/7 d window. Past the window we give up → caller falls back to
    the estimate.
    """
    if provider is None:
        return None
    fn = _USAGE_ADAPTERS.get(provider.usage_source)
    if not fn:
        return None
    fresh = fn(provider)
    if fresh is not None:
        _last_usage[provider.name] = (time.monotonic(), fresh)
        return fresh
    cached = _last_usage.get(provider.name)
    if cached and time.monotonic() - cached[0] < _USAGE_RETAIN:
        return cached[1]
    return None


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
                        # input = conversation at request time; +output = the
                        # post-turn fill (what the gauge should reflect).
                        ctx = (
                            int(usage.get("input_tokens", 0))
                            + int(usage.get("cache_read_input_tokens", 0))
                            + int(usage.get("cache_creation_input_tokens", 0))
                            + int(usage.get("output_tokens", 0))
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
        "session_pct": None,
        "weekly_pct": None,
        "reset_at": None,
        "source": "none",
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
    agy_ts, agy_model, agy_ctx = _scan_agy_log()
    if agy_model and (not model or model.startswith("<") or (agy_ts and agy_ts > newest_mtime)):
        model = agy_model
        ctx = agy_ctx
        if agy_ts:
            idle = (now - agy_ts) > timedelta(minutes=_IDLE_MIN)

    provider, rx = _match_provider(model, CONFIG.providers)
    name = provider.name if provider else "?"
    window_h = provider.window_hours if provider else _DEFAULT_WINDOW
    # Denominator: the model's real limit (per-model table), clamped to the
    # largest context we've ever seen it accept so a stale table entry can't
    # make the gauge read >100 % (proof: if it accepted N, its limit is ≥ N).
    resolved = _resolve_context_limit(model, provider.context_limit if provider else _DEFAULT_LIMIT)
    if model:
        _max_seen[model] = max(_max_seen.get(model, 0), ctx)
    limit = max(resolved, _max_seen.get(model, 0))

    # Preferred: the provider's official usage API (calibrated vs /usage).
    usage = _fetch_usage(provider)
    if usage is not None:
        session_pct, weekly_pct = usage["session_pct"], usage["weekly_pct"]
        reset_iso, source = usage["reset_at"], "api"
    else:
        # Fallback: JSONL block heuristic — an ESTIMATE (drifts vs the real
        # billing window anchor); events = active session + provider siblings.
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
        _, reset_at = _current_block(events, window_h)
        session_pct, weekly_pct = None, None
        reset_iso = reset_at.isoformat() if reset_at else None
        source = "estimate"

    pct = round(ctx / limit * 100) if limit else 0
    return {
        "provider": name,
        "model": model or "",
        "context_tokens": ctx,
        "context_limit": limit,
        "context_pct": pct,
        "session_pct": session_pct,
        "weekly_pct": weekly_pct,
        "reset_at": reset_iso,
        "source": source,
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
