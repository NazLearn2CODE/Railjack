"""Pure-Python secp256k1 + BIP-340 Schnorr signing — no C extension.

Buzz (the relay) verifies events with Rust's `nostr` crate, a real BIP-340
implementation, so this has to be bit-exact. Deliberately NOT using
`coincurve`/`secp256k1` bindings: this machine's Python (3.14, bleeding
edge) has no prebuilt wheel for either yet, and the sdist build fails —
same class of problem as the webkit2gtk system-header wall hit earlier in
the Buzz pilot. Pure Python has no build step, ever, on any machine.

Not fast (naive double-and-add), but signing one chat message is not a hot
loop — irrelevant here.

Reference: https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki
"""

from __future__ import annotations

import hashlib
import secrets

# secp256k1 curve parameters
P = 0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_FFFFFC2F
N = 0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_BAAEDCE6_AF48A03B_BFD25E8C_D0364141
Gx = 0x79BE667E_F9DCBBAC_55A06295_CE870B07_029BFCDB_2DCE28D9_59F2815B_16F81798
Gy = 0x483ADA77_26A3C465_5DA4FBFC_0E1108A8_FD17B448_A6855419_9C47D08F_FB10D4B8
G = (Gx, Gy)


def _mod_inv(a: int, m: int) -> int:
    return pow(a, m - 2, m)


def _point_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = (3 * p1[0] * p1[0]) * _mod_inv(2 * p1[1], P) % P
    else:
        lam = (p2[1] - p1[1]) * _mod_inv(p2[0] - p1[0], P) % P
    x3 = (lam * lam - p1[0] - p2[0]) % P
    y3 = (lam * (p1[0] - x3) - p1[1]) % P
    return (x3, y3)


def _point_mul(point: tuple[int, int], scalar: int) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _has_even_y(point: tuple[int, int]) -> bool:
    return point[1] % 2 == 0


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def _bytes_from_int(n: int) -> bytes:
    return n.to_bytes(32, "big")


def _int_from_bytes(b: bytes) -> int:
    return int.from_bytes(b, "big")


def pubkey_from_privkey(privkey_hex: str) -> str:
    """x-only public key (32-byte hex, BIP-340 / Nostr convention)."""
    d = int(privkey_hex, 16)
    if not (1 <= d < N):
        raise ValueError("private key out of range")
    point = _point_mul(G, d)
    assert point is not None
    return _bytes_from_int(point[0]).hex()


def schnorr_sign(privkey_hex: str, msg32: bytes, aux_rand32: bytes | None = None) -> str:
    """BIP-340 Schnorr sign. `msg32` must be exactly 32 bytes (a hash)."""
    if len(msg32) != 32:
        raise ValueError("msg32 must be 32 bytes")
    if aux_rand32 is None:
        aux_rand32 = secrets.token_bytes(32)

    d0 = int(privkey_hex, 16)
    if not (1 <= d0 < N):
        raise ValueError("private key out of range")

    P_point = _point_mul(G, d0)
    assert P_point is not None
    d = d0 if _has_even_y(P_point) else N - d0

    t = _bytes_from_int(d ^ _int_from_bytes(_tagged_hash("BIP0340/aux", aux_rand32)))
    rand = _tagged_hash("BIP0340/nonce", t + _bytes_from_int(P_point[0]) + msg32)
    k0 = _int_from_bytes(rand) % N
    if k0 == 0:
        raise ValueError("invalid nonce (k0=0), retry with different aux_rand")

    R = _point_mul(G, k0)
    assert R is not None
    k = k0 if _has_even_y(R) else N - k0

    e = _int_from_bytes(
        _tagged_hash("BIP0340/challenge", _bytes_from_int(R[0]) + _bytes_from_int(P_point[0]) + msg32)
    ) % N

    sig = _bytes_from_int(R[0]) + _bytes_from_int((k + e * d) % N)
    return sig.hex()


def schnorr_verify(pubkey_hex: str, msg32: bytes, sig_hex: str) -> bool:
    """Self-check only (Buzz's own verification via the `nostr` crate is the
    real authority) — used in this module's tests, not on the request path."""
    try:
        px = int(pubkey_hex, 16)
        sig = bytes.fromhex(sig_hex)
        if len(sig) != 64 or len(msg32) != 32:
            return False
        r = _int_from_bytes(sig[:32])
        s = _int_from_bytes(sig[32:])
        if r >= P or s >= N:
            return False
        P_point = _lift_x(px)
        if P_point is None:
            return False
        e = _int_from_bytes(
            _tagged_hash("BIP0340/challenge", sig[:32] + _bytes_from_int(px) + msg32)
        ) % N
        R = _point_add(_point_mul(G, s), _point_mul(P_point, (N - e) % N))
        if R is None or not _has_even_y(R) or R[0] != r:
            return False
        return True
    except Exception:
        return False


def _lift_x(x: int) -> tuple[int, int] | None:
    if x >= P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        return None
    if y % 2 != 0:
        y = P - y
    return (x, y)


def generate_privkey_hex() -> str:
    while True:
        candidate = secrets.token_bytes(32)
        d = _int_from_bytes(candidate)
        if 1 <= d < N:
            return candidate.hex()
