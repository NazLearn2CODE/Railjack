"""GET /api/catalog — cockpit skills, marketplace skills + MCP servers.

Every entry is ``{name, insert, group}``.

``skills`` = dirs under ``~/.claude/skills/`` (insert text = ``/name ``, the
slash invocation); grouped by the config regex rules.
``marketplace_skills`` = skills shipped by installed plugins (dirs under each
plugin's ``skills/`` that contain a ``SKILL.md``), read from
``~/.claude/plugins/installed_plugins.json`` → each entry's ``installPath``;
grouped by plugin name (marketing first — it's the big set). Insert = the
fully-qualified ``/plugin:name `` slash form (always unambiguous, unlike a bare
``/name`` that could collide with a personal skill).
``mcps`` = ``mcpServers`` keys merged from ``~/.claude.json``: the top-level
object plus every ``projects.<path>.mcpServers`` scope, deduped by name (insert
text from the configurable template); grouped by the config regex rules.

Config-rule grouping is first-match-wins with fallback ``OTHER``. 60 s
in-process cache. Paths are module-level constants so tests can monkeypatch them.
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
INSTALLED_PLUGINS_JSON = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

# Plugins to surface first in the marketplace menu (biggest / most-used sets),
# in this order; any other installed plugin follows, name-sorted.
_PLUGIN_ORDER = ("marketing-skills", "superpowers", "ponytail")

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


def _installed_plugin_roots() -> list[tuple[str, Path]]:
    """(plugin_name, installPath) per installed plugin, ordered ``_PLUGIN_ORDER``
    first then name-sorted. Keys in installed_plugins.json are ``name@marketplace``.
    """
    try:
        data = json.loads(INSTALLED_PLUGINS_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return []
    roots: list[tuple[str, Path]] = []
    for key, entries in plugins.items():
        name = key.split("@", 1)[0]
        if not isinstance(entries, list):
            continue
        path = next(
            (e["installPath"] for e in entries if isinstance(e, dict) and e.get("installPath")),
            None,
        )
        if path:
            roots.append((name, Path(path)))

    def _rank(item: tuple[str, Path]) -> tuple[int, str]:
        n = item[0]
        return (_PLUGIN_ORDER.index(n) if n in _PLUGIN_ORDER else len(_PLUGIN_ORDER), n)

    return sorted(roots, key=_rank)


def _marketplace_skills() -> list[dict]:
    """Skills shipped by installed plugins, grouped by plugin (uppercased).

    A plugin skill is a ``skills/<name>/`` dir with a ``SKILL.md``. Insert = the
    fully-qualified ``/plugin:name `` form (Claude Code's unambiguous namespace
    for plugin skills, e.g. ``/code-review:code-review``). Deduped by (plugin, name).
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for plugin, root in _installed_plugin_roots():
        sdir = root / "skills"
        if not sdir.is_dir():
            continue
        for p in sorted(sdir.iterdir()):
            if not (p.is_dir() and (p / "SKILL.md").is_file()):
                continue
            key = (plugin, p.name)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {"name": p.name, "insert": f"/{plugin}:{p.name} ", "group": plugin.upper()}
            )
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
    return {
        "skills": _skills(),
        "marketplace_skills": _marketplace_skills(),
        "mcps": _mcps(),
    }


@router.get("/api/catalog")
def catalog() -> dict:
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL:
        return _cache[1]
    data = _build()
    _cache = (now, data)
    return data


@router.post("/api/catalog/reload")
def catalog_reload() -> dict:
    """Force a fresh scan of skills / marketplace plugins / MCP servers, bypassing
    the ``_CACHE_TTL`` cache. The ↻ CFG button calls this so a skill/plugin/MCP
    added since the hub started shows up immediately — no cache wait, no restart."""
    global _cache
    data = _build()
    _cache = (time.monotonic(), data)
    return data
