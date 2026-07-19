"""Per-machine module configuration: YAML load + pydantic models + selection.

Selection order: ``RAILJACK_CONFIG`` env override (matches a config file stem),
else the lowercased ``hostname -s`` is matched against each config's
``hostnames:`` list. A miss raises at import time so uvicorn fails fast with a
message listing the available configs.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import yaml
from pydantic import BaseModel

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


class HealthSpec(BaseModel):
    type: str
    url: str | None = None


class ManageSpec(BaseModel):
    type: str  # "systemd-user" | "command"
    unit: str | None = None
    extra_units: list[str] = []
    start: list[str] | None = None
    stop: list[str] | None = None
    log: list[str] | None = None
    start_timeout_s: int = 150


class Module(BaseModel):
    id: str
    title: str
    kind: str  # "iframe" | "panel"
    url: str | None = None
    panel: str | None = None
    health: HealthSpec | None = None
    manage: ManageSpec | None = None
    options: dict = {}


# M6 cockpit config — all optional with safe defaults so the grimoldi stub
# (tmux-only) and any minimal config stay valid without these keys.

class Provider(BaseModel):
    name: str
    model_match: str  # regex tested against a session's model string
    window_hours: float = 5
    context_limit: int = 200_000
    # M6.1: official usage API instead of the JSONL block heuristic.
    # "anthropic-oauth" reads the OAuth token Claude Code maintains in
    # ~/.claude/.credentials.json; "zai-quota" hits the rate-ck endpoint with
    # the key from `key_env`. "none" (default) → heuristic only.
    usage_source: str = "none"
    key_env: str | None = None


class Button(BaseModel):
    label: str
    insert: str  # text typed into the terminal (Naz-edited in YAML)


class CatalogGroup(BaseModel):
    name: str
    match: str  # regex; first matching group wins


class CatalogSpec(BaseModel):
    mcp_insert_template: str = "use the {name} MCP to "
    groups: list[CatalogGroup] = []


class DockSpec(BaseModel):
    """Phase F: optional always-visible bottom terminal dock. Its iframe points
    at the same ttyd URL as the TERMINAL module, so both clients attach to the
    one tmux session (`tmux new -A -s main`) — typing in one mirrors in the other."""

    title: str = "LIVE"
    url: str
    height: int = 220


class MachineConfig(BaseModel):
    machine: str
    hostnames: list[str]
    modules: list[Module]
    providers: list[Provider] = []
    buttons: list[Button] = []
    catalog: CatalogSpec = CatalogSpec()
    dock: DockSpec | None = None


def _available() -> list[Path]:
    return sorted(CONFIG_DIR.glob("*.yaml"))


def _load(path: Path) -> MachineConfig:
    return MachineConfig(**(yaml.safe_load(path.read_text()) or {}))


def select_config() -> MachineConfig:
    """Pick this machine's config (env override > hostname match)."""
    candidates = _available()
    names = ", ".join(p.stem for p in candidates) or "(none)"
    if not candidates:
        raise RuntimeError(f"No machine configs in {CONFIG_DIR}; add configs/<machine>.yaml.")

    override = os.environ.get("RAILJACK_CONFIG")
    if override:
        for p in candidates:
            if p.stem == override:
                return _load(p)
        raise RuntimeError(f"RAILJACK_CONFIG={override!r} matches no config. Available: {names}")

    host = socket.gethostname().split(".")[0].lower()
    for p in candidates:
        cfg = _load(p)
        if host in cfg.hostnames:
            return cfg
    raise RuntimeError(
        f"No config matches hostname {host!r}. Set RAILJACK_CONFIG or add it to a "
        f"config's hostnames. Available: {names}"
    )


# ponytail: resolve at import so a missing/ambiguous config fails the uvicorn
# boot loudly (the "clear startup error" requirement) rather than at first request.
CONFIG = select_config()
