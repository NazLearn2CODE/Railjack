"""Session telemetry — CTX / SES / WK / RESET for the sidebar strip.

This is now the home Railjack module, back-ported from Somatic's 2026-08-17 telemetry state (4 commits 97fc6d9/e8ed042/a3b3bf0/a2512b4):
- CTX comes from the newest Claude Code session JSONL on disk (last committed
  usage line: input + cache_read + cache_creation + output tokens).
- The denominator is a per-model limit table resolved from the LIVE model
  string every poll (a mid-session /model switch is automatic), lifted by an
  empirical clamp: once a model has accepted N tokens, its limit is ≥ N — a
  stale table entry can never push the gauge past 100%.
- SES/WK/RESET come only from official usage sources (anthropic OAuth /usage,
  z.ai quota endpoint, cco-usage.jsonl spend, Antigravity local quota RPC);
  on transient failures the last good reading is retained for 10 minutes.
  No estimates are shown.
- Secrets: tokens are read, used in a header, never logged or returned.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter

router = APIRouter()

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS = CLAUDE_DIR / "projects"

# Model-pattern → context limit. All with (^|/) prefix to match bare and openrouter-prefixed models.
_MODEL_CONTEXT_LIMITS: list[tuple[re.Pattern[str], int]] = [
    # glm-5.x: whole line runs the IndexShare 1M stack (5.2 + 5.3 confirmed on z.ai blog)
    (re.compile(r"(^|/)glm-5\.\d"), 1_000_000),
    (re.compile(r"(^|/)glm-5-turbo"), 1_000_000),
    (re.compile(r"(^|/)cco-glm"), 1_000_000),
    (re.compile(r"(^|/)gemini-"), 1_000_000),
    (re.compile(r"(^|/)claude-(fable|mythos)"), 1_000_000),
    (re.compile(r"(^|/)claude-haiku"), 200_000),
    (re.compile(r"(^|/)claude-"), 1_000_000),
]
_DEFAULT_LIMIT = 200_000

# provider detection from the model string (order matters: cco BEFORE zai)
_PROVIDERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|/)cco-"), "cco"),
    (re.compile(r"(^|/)glm-"), "zai"),
    (re.compile(r"(^|/)gemini-"), "gemini"),
    (re.compile(r"(^|/)claude-"), "claude"),
]

_max_seen: dict[str, int] = {}

_LAST_GOOD_TTL = 600.0
_usage_cache: dict[str, tuple[float, dict]] = {}

# A provider counts as "in use" if its newest session JSONL was written within
# this window (seconds) — drives a telemetry lane's green/red light.
ACTIVE_WINDOW = 90.0
# Model shown per provider before any session is ever seen, so a lane is
# populated on first boot.
_DEFAULT_MODELS: dict[str, str] = {"zai": "glm-5.2", "gemini": "gemini-3.6-flash", "claude": "claude-3.7-sonnet"}
_last_state: dict[str, dict] = {}


def _limit_for(model: str, used: int) -> int:
    limit = next((v for p, v in _MODEL_CONTEXT_LIMITS if p.search(model)), _DEFAULT_LIMIT)
    seen = _max_seen.get(model, 0)
    if used > seen:
        _max_seen[model] = seen = used
    return max(limit, seen)


def _provider_for(model: str) -> str:
    return next((name for p, name in _PROVIDERS if p.search(model)), "unknown")


GEMINI_DIR = Path.home() / ".gemini" / "antigravity-cli"


def _norm_ag_model(display: str) -> str:
    """'Gemini 3.7 Flash (Medium)' → 'gemini-3.7-flash' (drops effort suffix)."""
    return re.sub(r"\s*\([^)]*\)", "", display or "").strip().lower().replace(" ", "-")


_ag_model_cache: tuple[float, str] = (0.0, "")


def _agy_selected_model() -> str:
    """Antigravity's selected model, read live from its settings.json (5-min
    cache) — the client updates model versions ahead of our table."""
    global _ag_model_cache
    ts, model = _ag_model_cache
    if model and time.monotonic() - ts < 300:
        return model
    try:
        s = json.loads((GEMINI_DIR / "settings.json").read_text())
        m = _norm_ag_model(s.get("model", ""))
        if m:
            _ag_model_cache = (time.monotonic(), m)
            return m
    except Exception:
        pass
    return model or "gemini-3.6-flash"


def _scan_jsonl_usage(path: Path) -> tuple[str, int] | None:
    """(model, context_tokens) from the last assistant usage line in ``path``."""
    if GEMINI_DIR in path.parents or path.parent == GEMINI_DIR:
        return _agy_selected_model(), 0
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262_144))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = d.get("message") or {}
        u = msg.get("usage")
        model = msg.get("model")
        if u and model:
            used = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
            return model, used
    return None


def _per_provider_state() -> dict[str, dict]:
    """Newest session JSONLs grouped by provider, merged over the cached
    last-seen state so an idle provider keeps its model + ctx capacity.

    Returns ``{provider: {model, ctx_used, ctx_limit, last_mtime}}``.
    """
    jsonls: list[Path] = []
    try:
        jsonls.extend(PROJECTS.glob("*/*.jsonl"))
    except Exception:
        pass
    try:
        if GEMINI_DIR.exists():
            jsonls.extend(GEMINI_DIR.glob("history.jsonl"))
            jsonls.extend(GEMINI_DIR.glob("brain/**/transcript.jsonl"))
    except Exception:
        pass
    try:
        jsonls = sorted(jsonls, key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        jsonls = []

    state: dict[str, dict] = {k: dict(v) for k, v in _last_state.items()}
    for path in jsonls[:15]:  # newest covers recent multi-provider activity
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        found = _scan_jsonl_usage(path)
        if not found:
            continue
        model, used = found
        provider = _provider_for(model)
        prev = state.get(provider)
        if prev is None or mtime > prev.get("last_mtime", 0):
            state[provider] = {
                "model": model,
                "ctx_used": used,
                "ctx_limit": _limit_for(model, used),
                "last_mtime": mtime,
            }
    _last_state.clear()
    _last_state.update(state)
    return state


# ── official usage APIs (SES / WK / RESET) ──────────────────────────────

async def _anthropic_usage() -> dict | None:
    """The OAuth /usage endpoint Claude Code itself polls. Tightly
    rate-limited (intermittent 429s) → last-good retention."""
    try:
        creds = json.loads((CLAUDE_DIR / ".credentials.json").read_text())
        token = creds["claudeAiOauth"]["accessToken"]
    except Exception:
        return None
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}",
                     "anthropic-beta": "oauth-2025-04-20"},
        )
        resp.raise_for_status()
        data = resp.json()
    out: dict = {}
    five = data.get("five_hour") or {}
    week = data.get("seven_day") or {}
    if "utilization" in five:
        out["session_pct"] = round(float(five["utilization"]))
        if five.get("resets_at"):
            out["reset_at"] = five["resets_at"]
    if "utilization" in week:
        out["week_pct"] = round(float(week["utilization"]))
    return out or None


def _parse_zai_usage(resp: dict) -> dict | None:
    """SES/RESET from a z.ai /monitor/usage/quota/limit response."""
    data = (resp or {}).get("data") or {}
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        return None
    tok_q = next((it for it in limits
                  if isinstance(it, dict) and str(it.get("type", "")).upper() == "TOKENS_LIMIT"), None)
    if not tok_q or tok_q.get("percentage") is None or not tok_q.get("nextResetTime"):
        return None
    return {
        "session_pct": round(float(tok_q["percentage"])),
        "reset_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(tok_q["nextResetTime"]) / 1000)),
    }


async def _zai_usage() -> dict | None:
    """z.ai quota endpoint (see vault: zai-quota-monitoring)."""
    key = os.environ.get("ZAI_API_KEY")
    if not key:
        return None
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            "https://api.z.ai/api/monitor/usage/quota/limit",
            headers={"Authorization": f"Bearer {key}"},
        )
        resp.raise_for_status()
        return _parse_zai_usage(resp.json())


def _usage_cco_spend() -> dict | None:
    """Sum cco-usage.jsonl month-to-date cost vs $5 cap."""
    log_path = CLAUDE_DIR / "cco-usage.jsonl"
    if not log_path.exists():
        return None
    now_dt = datetime.now()
    month_start = datetime(now_dt.year, now_dt.month, 1).timestamp()
    month_cost = 0.0
    try:
        with open(log_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts", 0)
                if ts >= month_start:
                    month_cost += rec.get("cost", 0.0) or 0.0
    except Exception:
        return None
    cap = 5.0
    pct = min(100, round((month_cost / cap) * 100))
    if now_dt.month == 12:
        next_month = datetime(now_dt.year + 1, 1, 1)
    else:
        next_month = datetime(now_dt.year, now_dt.month + 1, 1)
    return {
        "session_pct": pct,
        "reset_at": next_month.strftime("%Y-%m-%dT00:00:00Z"),
    }


def _parse_ag_groups(data: dict) -> dict[str, dict]:
    """Parse a RetrieveUserQuotaSummary response into per-group usage dicts:
    ``{"gemini": {...}, "3p": ...}`` — each carries the 5h window
    (session_pct + reset_at) and the weekly window (week_pct + week_reset_at),
    inverted from remainingFraction (dashboard shows remaining, we show used)."""
    out: dict[str, dict] = {}
    for g in (data or {}).get("groups") or []:
        name = (g.get("displayName") or "").lower()
        key = ("gemini" if "gemini" in name
               else "3p" if "claude" in name or "gpt" in name else None)
        if not key or key in out:
            continue
        lane: dict = {}
        for b in g.get("buckets") or []:
            if b.get("remainingFraction") is None:
                continue
            w = f"{b.get('window', '')} {b.get('bucketId', '')}"
            used = round((1.0 - float(b["remainingFraction"])) * 100)
            if "5h" in w:
                lane["session_pct"] = used
                if b.get("resetTime"):
                    lane["reset_at"] = b["resetTime"]
            elif "week" in w:
                lane["week_pct"] = used
                if b.get("resetTime"):
                    lane["week_reset_at"] = b["resetTime"]
        if lane:
            out[key] = lane
    return out


_ag_quota_cache: tuple[float, dict] = (0.0, {})

# Last daemon reading persisted to disk: while no Antigravity session runs,
# weekly usage cannot change, so a cached week reading stays true between
# runs (capped anyway); the 5h window rolls on wall time, so its reading
# expires as fast as the in-memory one.
_AG_CACHE_FILE = Path.home() / ".cache" / "railjack" / "ag-quota.json"
_AG_SESSION_TTL = 600.0
_AG_WEEK_TTL = 6 * 3600.0


def _save_ag_disk(groups: dict[str, dict]) -> None:
    try:
        _AG_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _AG_CACHE_FILE.write_text(json.dumps({"ts": time.time(), "groups": groups}))
    except Exception:
        pass


def _load_ag_disk() -> dict[str, dict]:
    """Persisted daemon quota with per-field TTL applied."""
    try:
        d = json.loads(_AG_CACHE_FILE.read_text())
        age = time.time() - float(d.get("ts", 0))
        out: dict[str, dict] = {}
        for key, lane in (d.get("groups") or {}).items():
            fresh: dict = {}
            if age < _AG_SESSION_TTL:
                for k in ("session_pct", "reset_at"):
                    if k in lane:
                        fresh[k] = lane[k]
            if age < _AG_WEEK_TTL:
                for k in ("week_pct", "week_reset_at"):
                    if k in lane:
                        fresh[k] = lane[k]
            if fresh:
                out[key] = fresh
        return out
    except Exception:
        return {}


async def _antigravity_quota() -> dict[str, dict]:
    """Both model-group quotas from the local Antigravity LanguageServer
    Connect-RPC (60s cache, shared by the gemini + claude lanes — one RPC per
    minute, not one per lane). Empty dict on failure; callers fall back."""
    global _ag_quota_cache
    ts, groups = _ag_quota_cache
    if groups and time.monotonic() - ts < 60:
        return groups
    try:
        import subprocess

        # Discover local LanguageServerService / agy Connect-RPC ports
        ports: set[int] = set()
        try:
            out = subprocess.run(["ss", "-tlpn"], capture_output=True, text=True, timeout=1).stdout
            for line in out.splitlines():
                if "agy" in line or "language_server" in line:
                    m = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if m:
                        ports.add(int(m.group(1)))
        except Exception:
            pass

        if not ports:
            try:
                with open("/proc/net/tcp", "r") as f:
                    for line in f.readlines()[1:]:
                        parts = line.strip().split()
                        if len(parts) > 3 and parts[3] == "0A":
                            local_addr, local_port_hex = parts[1].split(":")
                            if local_addr in ("0100007F", "00000000"):
                                p = int(local_port_hex, 16)
                                if p > 1024 and p not in (8700, 11434, 27124, 4127, 4128, 3456, 19825, 7681):
                                    ports.add(p)
            except Exception:
                pass

        async with httpx.AsyncClient(timeout=1.0) as client:
            for port in sorted(ports):
                try:
                    r = await client.post(
                        f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
                        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
                        json={},
                    )
                    if r.status_code == 200:
                        parsed = _parse_ag_groups(r.json().get("response") or r.json())
                        if parsed:
                            _ag_quota_cache = (time.monotonic(), parsed)
                            _save_ag_disk(parsed)
                            return parsed
                except Exception:
                    continue
    except Exception:
        pass
    return {}


async def _gemini_usage() -> dict | None:
    """Google / Gemini quota (AI-Pro): 5h + weekly from the local Antigravity
    LanguageServer RPC; then the persisted last daemon reading (week fields
    outlive the run); then upstream retrieveUserQuota (daily REQUEST buckets
    only) via the keyring bearer token."""
    groups = await _antigravity_quota()
    if groups.get("gemini"):
        return groups["gemini"]
    disk = _load_ag_disk()
    if disk.get("gemini"):
        return disk["gemini"]
    try:
        import secretstorage

        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        items = list(collection.search_items({"service": "gemini", "username": "antigravity"}))
        if not items:
            return None
        secret = json.loads(items[0].get_secret())
        token = secret.get("token", {}).get("access_token")
        if not token:
            return None

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={},
            )
            if resp.status_code == 200:
                data = resp.json()
                b = min(data.get("buckets") or [],
                        key=lambda x: float(x.get("remainingFraction", 1.0)), default=None)
                if b is not None and b.get("remainingFraction") is not None:
                    out = {"session_pct": round((1.0 - float(b["remainingFraction"])) * 100)}
                    if b.get("resetTime"):
                        out["reset_at"] = b["resetTime"]
                    return out
    except Exception:
        pass
    return None


async def _google_3p_usage() -> dict | None:
    """Claude+GPT group hosted on the Google AI-Pro sub, from the same local
    Antigravity RPC (5h + weekly), then the persisted last daemon reading.
    None beyond that → the claude lane falls back to anthropic-oauth."""
    groups = await _antigravity_quota()
    if groups.get("3p"):
        return groups["3p"]
    return _load_ag_disk().get("3p")


if __name__ == "__main__":
    import sys
    try:
        import secretstorage
        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        items = list(collection.search_items({"service": "gemini", "username": "antigravity"}))
        if not items:
            print("ERROR: No keyring item found (service='gemini', username='antigravity')")
            sys.exit(1)
        secret = json.loads(items[0].get_secret())
        token = secret.get("token", {})
        access_token = token.get("access_token", "")
        expiry = token.get("expiry", "")
        print(f"Token length: {len(access_token)} chars")
        print(f"Token expiry: {expiry}")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


async def _provider_usage(provider: str) -> dict:
    """Fetch with 10-min last-good retention across transient failures."""
    now = time.monotonic()
    try:
        if provider == "anthropic" or provider == "claude":
            # Google-sub Claude/GPT quota first (the CLAUDE / GPT lane); native
            # Anthropic OAuth as fallback when the Antigravity daemon is down.
            data = await _google_3p_usage() or await _anthropic_usage()
        elif provider == "zai":
            data = await _zai_usage()
        elif provider == "cco":
            data = _usage_cco_spend()
        elif provider == "gemini":
            data = await _gemini_usage()
        else:
            data = None

        if data:
            _usage_cache[provider] = (now, data)
            return data
    except Exception:
        pass
    ts, cached = _usage_cache.get(provider, (0.0, {}))
    if now - ts < _LAST_GOOD_TTL and cached:
        return cached
    return {}  # no real reading + no fresh cache → show nothing, never fabricate


async def session_payload() -> dict:
    """Per-provider telemetry lanes for the sidebar strip.

    Each lane is always populated with model + ctx capacity + quota used-% +
    reset timer (quota data survives idle via ``_provider_usage``'s 10-min
    last-good retention); ``ctx_pct`` (live context-window fill) appears only
    when the provider is ``active`` (a session writing within ACTIVE_WINDOW).
    ``active`` alone drives the green/red light — the rest stays visible idle.
    """
    now = time.time()
    state = _per_provider_state()
    lanes: dict[str, dict] = {}
    for provider in ("gemini", "claude", "zai"):
        st = state.get(provider)
        active = bool(st and (now - st.get("last_mtime", 0)) <= ACTIVE_WINDOW)
        model = st["model"] if st else _DEFAULT_MODELS.get(provider)
        ctx_limit = st["ctx_limit"] if st else (_limit_for(model, 0) if model else None)
        lane: dict = {"active": active}
        if model:
            lane["model"] = model
        if ctx_limit:
            lane["ctx_limit"] = ctx_limit
        if st and active and ctx_limit:
            lane["ctx_pct"] = round(st["ctx_used"] / ctx_limit * 100)
        usage = await _provider_usage(provider)
        if usage.get("session_pct") is not None:
            lane["used_pct"] = usage["session_pct"]
        if usage.get("week_pct") is not None:
            lane["week_pct"] = usage["week_pct"]
        if usage.get("week_reset_at"):
            lane["week_reset_at"] = usage["week_reset_at"]
        if usage.get("reset_at"):
            lane["reset_at"] = usage["reset_at"]
        lanes[provider] = lane
    return {"lanes": lanes}


@router.get("/api/session")
async def session() -> dict:
    return await session_payload()
