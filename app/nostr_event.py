"""NIP-01 event construction: canonical serialization, id, BIP-340 signature.

Mirrors what `nostr::EventBuilder::sign_with_keys` does in the Rust `nostr`
crate that Buzz's relay verifies against — see ``nostr_crypto.py`` for the
signing primitives themselves.
"""

from __future__ import annotations

import hashlib
import json
import time

from .nostr_crypto import pubkey_from_privkey, schnorr_sign


def _serialize_for_id(pubkey: str, created_at: int, kind: int, tags: list, content: str) -> bytes:
    # NIP-01: [0, pubkey, created_at, kind, tags, content] — compact JSON,
    # UTF-8 kept raw (ensure_ascii=False), no extra whitespace.
    arr = [0, pubkey, created_at, kind, tags, content]
    return json.dumps(arr, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_event(
    privkey_hex: str,
    kind: int,
    content: str,
    tags: list[list[str]] | None = None,
    created_at: int | None = None,
) -> dict:
    """Build and sign a Nostr event. Returns the full event dict ready to POST."""
    pubkey = pubkey_from_privkey(privkey_hex)
    tags = tags or []
    ts = created_at if created_at is not None else int(time.time())
    serialized = _serialize_for_id(pubkey, ts, kind, tags, content)
    event_id = hashlib.sha256(serialized).hexdigest()
    sig = schnorr_sign(privkey_hex, bytes.fromhex(event_id))
    return {
        "id": event_id,
        "pubkey": pubkey,
        "created_at": ts,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
    }
