"""BIP-340/NIP-01 correctness — this module must stay bit-exact with the
Rust `nostr` crate Buzz's relay verifies against, or every signed event
gets silently rejected. Anchored to the official BIP-340 test vector #0
(privkey=3) rather than only self-consistency, since a matched bug in both
sign and verify wouldn't be caught by round-tripping against itself."""

from app.nostr_crypto import (
    generate_privkey_hex,
    pubkey_from_privkey,
    schnorr_sign,
    schnorr_verify,
)
from app.nostr_event import build_event

BIP340_VECTOR_0_PRIVKEY = "0000000000000000000000000000000000000000000000000000000000000003"
BIP340_VECTOR_0_PUBKEY = "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"


def test_pubkey_matches_official_bip340_test_vector():
    assert pubkey_from_privkey(BIP340_VECTOR_0_PRIVKEY) == BIP340_VECTOR_0_PUBKEY


def test_sign_verify_round_trip():
    priv = generate_privkey_hex()
    pub = pubkey_from_privkey(priv)
    msg = b"\x01" * 32
    sig = schnorr_sign(priv, msg)
    assert schnorr_verify(pub, msg, sig)


def test_verify_rejects_wrong_message():
    priv = generate_privkey_hex()
    pub = pubkey_from_privkey(priv)
    sig = schnorr_sign(priv, b"\x01" * 32)
    assert not schnorr_verify(pub, b"\x02" * 32, sig)


def test_verify_rejects_wrong_pubkey():
    priv = generate_privkey_hex()
    other_pub = pubkey_from_privkey(generate_privkey_hex())
    msg = b"\x03" * 32
    sig = schnorr_sign(priv, msg)
    assert not schnorr_verify(other_pub, msg, sig)


def test_build_event_produces_valid_signature_over_its_own_id():
    priv = generate_privkey_hex()
    pub = pubkey_from_privkey(priv)
    event = build_event(priv, kind=9, content="hello", tags=[["h", "abc"]])
    assert event["pubkey"] == pub
    assert schnorr_verify(pub, bytes.fromhex(event["id"]), event["sig"])
