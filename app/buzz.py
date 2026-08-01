"""BUZZ panel — sister-to-sister + Naz chat, backed by a self-hosted Buzz
relay (Nostr NIP-29 groups). Talks to the relay's REST bridge directly via
``buzz_client.BuzzClient`` (pure-Python NIP-01/NIP-98, no Rust/Tauri client
involved) — see [[buzz-agent-workspace]] / [[buzz-module-for-railjack]] in
the Cephalon vault for why this exists and why it's a Railjack panel and not
a standalone app.

Identity and the bootstrapped channel id are persisted under
``~/.config/railjack/`` (same convention as the newsroom Google token),
generated on first use.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from .buzz_client import BuzzClient, BuzzError
from .nostr_crypto import generate_privkey_hex, pubkey_from_privkey
from .config import CONFIG

router = APIRouter()

STATE_DIR = Path.home() / ".config" / "railjack"
STATE_FILE = STATE_DIR / "buzz_state.json"

# Multiple local identities can share this one Railjack instance (e.g. the
# machine's sister persona plus Naz typing as himself) — each gets its own
# keypair file, selected via the `as` query param. "sister" (the machine's
# own persona, from config's display_name) is the default so existing
# callers with no `as` param are unaffected.
_IDENTITIES: dict[str, str] = {"sister": "buzz_identity.json", "naz": "buzz_identity_naz.json"}


def _module_options() -> dict:
    for m in CONFIG.modules:
        if m.id == "buzz":
            return m.options
    return {}


def _relay_url() -> str:
    return str(_module_options().get("relay_url") or "http://localhost:3000")


def _channel_name() -> str:
    return str(_module_options().get("channel_name") or "sisters-lounge")


def _display_name(identity: str) -> str:
    if identity == "naz":
        return "Naz"
    return str(_module_options().get("display_name") or CONFIG.machine)


def _identity_file(identity: str) -> Path:
    if identity not in _IDENTITIES:
        raise HTTPException(400, f"unknown identity {identity!r} (known: {sorted(_IDENTITIES)})")
    return STATE_DIR / _IDENTITIES[identity]


def _load_privkey(identity: str) -> str:
    """Return this identity's Buzz private key (hex), generating one on first use."""
    path = _identity_file(identity)
    if path.exists():
        return json.loads(path.read_text())["private_key_hex"]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    priv = generate_privkey_hex()
    path.write_text(json.dumps({"private_key_hex": priv}))
    path.chmod(0o600)
    return priv


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


_clients: dict[str, BuzzClient] = {}


def _get_client(identity: str) -> BuzzClient:
    if identity not in _clients:
        _clients[identity] = BuzzClient(_relay_url(), _load_privkey(identity))
    return _clients[identity]


async def _ensure_channel(client: BuzzClient) -> str:
    state = _load_state()
    channel_id = state.get("channel_id")
    if channel_id:
        return channel_id
    channel_id = await client.create_channel(_channel_name(), about="Railjack BUZZ panel — auto-bootstrapped")
    _save_state({**state, "channel_id": channel_id})
    return channel_id


@router.get("/api/buzz/identities")
async def api_identities():
    """Which local identities exist on this machine — drives the panel's identity switcher."""
    return {"identities": sorted(_IDENTITIES), "default": "sister"}


@router.get("/api/buzz/state")
async def api_state(identity: str = "sister"):
    """Identity + channel + relay reachability — drives the panel's header."""
    client = _get_client(identity)
    pubkey = pubkey_from_privkey(client.privkey_hex)
    try:
        channel_id = await _ensure_channel(client)
        reachable = True
        error = None
    except Exception as e:  # relay down, network error, etc.
        channel_id = _load_state().get("channel_id")
        reachable = False
        error = str(e)
    return {
        "identity": identity,
        "pubkey": pubkey,
        "display_name": _display_name(identity),
        "relay_url": _relay_url(),
        "channel_id": channel_id,
        "channel_name": _channel_name(),
        "reachable": reachable,
        "error": error,
    }


@router.get("/api/buzz/messages")
async def api_get_messages(identity: str = "sister", since: int | None = None, limit: int = 100):
    client = _get_client(identity)
    try:
        channel_id = await _ensure_channel(client)
        events = await client.get_messages(channel_id, limit=limit, since=since)
    except BuzzError as e:
        raise HTTPException(502, f"relay error: {e.body}")
    except Exception as e:
        raise HTTPException(502, f"relay unreachable: {e}")
    return {
        "channel_id": channel_id,
        "messages": [
            {
                "id": ev.get("id"),
                "pubkey": ev.get("pubkey"),
                "content": ev.get("content"),
                "created_at": ev.get("created_at"),
            }
            for ev in events
        ],
    }


@router.post("/api/buzz/messages")
async def api_send_message(body: dict = Body(...)):
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content required")
    client = _get_client(str(body.get("identity") or "sister"))
    try:
        channel_id = await _ensure_channel(client)
        result = await client.send_message(channel_id, content)
    except BuzzError as e:
        raise HTTPException(502, f"relay error: {e.body}")
    except Exception as e:
        raise HTTPException(502, f"relay unreachable: {e}")
    return result
