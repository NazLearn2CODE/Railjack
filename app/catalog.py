"""GET /api/catalog — cockpit skills + MCP servers, each ``{name, insert, group}``.

Skills = dirs under ``~/.claude/skills/`` (insert text = ``/name ``, the slash
invocation). MCPs = ``mcpServers`` keys merged from ``~/.claude.json``: the
top-level object plus every ``projects.<path>.mcpServers`` scope, deduped by
name (insert text from the configurable template). Grouping: ordered regex
rules from config, first match wins, fallback ``OTHER``. 60 s in-process cache.

Paths are module-level constants so tests can monkeypatch them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi import APIRouter

from .config import CONFIG

router = APIRouter()

SKILLS_DIR = Path.home() / ".claude" / "skills"
CLAUDE_JSON = Path.home() / ".claude.json"

_CACHE_TTL = 60
_OTHER = "OTHER"
_cache: tuple[float, dict] | None = None


def _group(name: str) -> str:
    for g in CONFIG.catalog.groups:
        if re.search(g.match, name):
            return g.name
    return _OTHER


def _skills() -> list[dict]:
    if not SKILLS_DIR.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(SKILLS_DIR.iterdir()):
        if p.is_dir():
            n = p.name
            out.append({"name": n, "insert": f"/{n} ", "group": _group(n)})
    return out


def _mcp_names() -> list[str]:
    """Global + all project-scope mcpServers keys, deduped, insertion-ordered."""
    if not CLAUDE_JSON.is_file():
        return []
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []

    names: list[str] = []
    seen: set[str] = set()

    def _add(src: object) -> None:
        if not isinstance(src, dict):
            return
        for n in src:
            if n not in seen:
                seen.add(n)
                names.append(n)

    _add(data.get("mcpServers"))
    projects = data.get("projects")
    if isinstance(projects, dict):
        for path, pcfg in projects.items():
            if isinstance(pcfg, dict):
                _add(pcfg.get("mcpServers"))
            # Project-scoped servers can also live in <project>/.mcp.json
            # (e.g. the Cephalon vault's `obsidian` server) — merge those too.
            mcp_file = Path(path) / ".mcp.json"
            try:
                pdata = json.loads(mcp_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(pdata, dict):
                _add(pdata.get("mcpServers"))
    return names


def _mcps() -> list[dict]:
    tpl = CONFIG.catalog.mcp_insert_template
    return [
        {"name": n, "insert": tpl.replace("{name}", n), "group": _group(n)}
        for n in _mcp_names()
    ]


def _build() -> dict:
    return {"skills": _skills(), "mcps": _mcps()}


@router.get("/api/catalog")
def catalog() -> dict:
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    data = _build()
    _cache = (now, data)
    return data
