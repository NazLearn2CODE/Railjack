"""JEV credit lane — dollars-remaining gauge for the TypeSafe $5 monthly refill.

Naz spec (2026-09-19): same treatment as the GLM/Gemini telemetry lanes, but
the gauge is DOLLARS, not percentage.

Why jsonl-sum and not an API: TypeSafe exposes per-request `usage`
(input/output tokens) but no balance/usage endpoint. Every Jev call in the
house goes through jev_meter.py, which appends one JSON line per call to
~/.hermes/jev_usage.jsonl — that ledger IS the ground truth, summed here the
same way _usage_cco_spend sums cco-usage.jsonl.

Credit window: grant day is the 18th (UTC) — the window runs 18th→18th,
NOT calendar month. Window start = most recent 18th ≤ now.

Response (GET /api/jev):
    spend_usd      float   metered spend in the current credit window
    remaining_usd  float   5.00 − spend_usd  (the gauge)
    refill_usd     5.00
    window_start   ISO     current credit window start (most recent 18th)
    reset_at       ISO     next 18th 00:00 UTC (credit expiry / re-grant day)
    calls          int     metered calls this window
    tokens_in/out  int     token totals this window
    sources        [str]   ledgers summed (local + any tailnet mirrors)

Multi-box: the hub sees only its own machine's ledger by default. Set
JEV_LEDGER_URLS (comma-separated http(s) URLs serving the raw jsonl — e.g.
a tailnet file server from home) to roll up every box that runs Jev.
No estimates: if a ledger is unreadable it is skipped and reported.
Secrets: the meter ledger holds token counts only — no keys, safe to serve.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

LOCAL_LEDGER = Path.home() / ".hermes" / "jev_usage.jsonl"
REFILL_USD = 5.00
PRICE_PER_MTOK = 0.042
GRANT_DAY = 18  # UTC day the monthly credit lands (Sep 18 observed)
# Balance anchor (Naz 2026-10-02): the ledger sum can never reproduce the true
# TypeSafe balance (pre-mirror history is invisible to every ledger we read).
# When an anchor exists, the gauge trusts ITS balance and counts only
# post-anchor ledger lines — machine-local spend delta on top of truth.
ANCHOR_PATH = Path.home() / ".config" / "railjack" / "jev-anchor.json"


def _credit_window(now: datetime) -> tuple[datetime, datetime]:
    """(window_start, next_reset) for the 18th→18th UTC credit cycle."""
    if now.day >= GRANT_DAY:
        # inside this cycle: window opened on the 18th, resets NEXT month
        start = now.replace(day=GRANT_DAY, hour=0, minute=0, second=0, microsecond=0)
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    else:
        # before the 18th: window opened LAST month, resets THIS month's 18th
        year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        start = now.replace(year=year, month=month, day=GRANT_DAY,
                            hour=0, minute=0, second=0, microsecond=0)
        reset = now.replace(year=now.year, month=now.month, day=GRANT_DAY,
                            hour=0, minute=0, second=0, microsecond=0)
        return start, reset
    reset = now.replace(year=year, month=month, day=GRANT_DAY,
                        hour=0, minute=0, second=0, microsecond=0)
    return start, reset


def _read_ledger_lines() -> tuple[list[dict], list[str]]:
    """All ledger lines reachable: local file + JEV_LEDGER_URLS mirrors."""
    import httpx

    lines: list[dict] = []
    sources: list[str] = []

    try:
        for raw in LOCAL_LEDGER.read_text().splitlines():
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        sources.append(str(LOCAL_LEDGER))
    except OSError:
        pass

    mirror_cache = Path.home() / ".cache" / "railjack" / "jev-mirror.jsonl"
    for url in [u.strip() for u in os.environ.get("JEV_LEDGER_URLS", "").split(",") if u.strip()]:
        try:
            r = httpx.get(url, timeout=4.0)
            r.raise_for_status()
            for raw in r.text.splitlines():
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
            sources.append(url)
            # last-good snapshot: a down mirror must not erase the metered
            # spend it contributed (Naz 2026-10-02: gauge jumped when orokin
            # went offline — metered credit vanished from the sum)
            try:
                mirror_cache.parent.mkdir(parents=True, exist_ok=True)
                mirror_cache.write_text(r.text)
            except OSError:
                pass
        except Exception:
            # mirror down → fall back to the last-good snapshot so the meter
            # keeps counting that machine's usage; marked degraded in sources
            if "mirror" not in sources and mirror_cache.exists():
                try:
                    cached = 0
                    for raw in mirror_cache.read_text().splitlines():
                        try:
                            lines.append(json.loads(raw))
                            cached += 1
                        except json.JSONDecodeError:
                            continue
                    if cached:
                        sources.append(f"{url} (cached, {cached} lines)")
                except OSError:
                    pass
            continue  # a down mirror never blocks the lane

    return lines, sources


@router.get("/api/jev")
async def jev() -> dict:
    now = datetime.now(timezone.utc)
    window_start, reset_at = _credit_window(now)

    calls = 0
    tokens_in = tokens_out = 0
    last_dt: datetime | None = None
    # to_thread: the ledger read does sync httpx.get per mirror (4s timeout
    # each). Inline, a dead mirror (orokin offline) froze the EVENT LOOP for
    # that whole timeout on every /api/session poll — every concurrent route
    # (NEWSROOM queue, catalog) stalled with it (2026-10-05).
    lines, sources = await asyncio.to_thread(_read_ledger_lines)
    anchor = None
    anchor_ts = None
    try:
        raw_anchor = json.loads(ANCHOR_PATH.read_text())
        anchor = float(raw_anchor["balance_usd"])
        anchor_ts = datetime.fromisoformat(raw_anchor["ts"])
        if anchor_ts.tzinfo is None:
            anchor_ts = anchor_ts.replace(tzinfo=timezone.utc)
    except Exception:
        anchor, anchor_ts = None, None
    for e in lines:
        ts = str(e.get("ts", ""))
        try:
            stamp = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if stamp < window_start:
            continue
        if anchor_ts is not None and stamp < anchor_ts:
            continue  # pre-anchor history is inside the anchor balance
        calls += 1
        tokens_in += e.get("in") or 0
        tokens_out += e.get("out") or 0
        try:
            if last_dt is None or stamp > last_dt:
                last_dt = stamp
        except TypeError:
            pass  # naive vs aware ts mixup — never block the lane

    spend_usd = round(tokens_in / 1_000_000 * PRICE_PER_MTOK, 4)

    if anchor is not None:
        # anchored to the TypeSafe dashboard balance — machine spend since the
        # anchor is the only delta the ledger can honestly add
        remaining = max(0.0, anchor - spend_usd)
    else:
        remaining = REFILL_USD - spend_usd

    return {
        "spend_usd": spend_usd,
        "remaining_usd": round(remaining, 4),
        "anchored": anchor is not None,
        "anchor_balance_usd": anchor,
        "refill_usd": REFILL_USD,
        "window_start": window_start.strftime("%Y-%m-%dT00:00:00Z"),
        "reset_at": reset_at.strftime("%Y-%m-%dT00:00:00Z"),
        "calls": calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "sources": sources,
        # ISO ts of the newest counted call — freshness pip for the sidebar lane
        "last_call_ts": last_dt.isoformat() if last_dt else None,
    }
