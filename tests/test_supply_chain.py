"""SUPPLY-01: publisher signatures + checksum chain.

Layer 1 pins Ed25519 itself against the official RFC 8032 test vectors.
Layer 2 exercises the product chain: signed catalog → per-entry sha256 →
per-entry artifact signature, including the attack each layer exists for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest

from oaset.supply import (
    canonical_json,
    ed25519_keygen,
    ed25519_sign,
    ed25519_verify,
    sign_payload,
    verify_artifact_signature,
    verify_signature_block,
)
from oaset.update import CatalogEntry, UpdateError, UpdateManager


class Cfg:
    network_mode = "pull_only"


class FakeResponse:
    def __init__(self, text: str):
        self.text = text


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ------------------------------------------------------- RFC 8032 test vectors

VECTOR_1 = {
    "seed": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
    "pub": "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
    "msg": "",
    "sig": "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
           "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
}

VECTOR_2 = {
    "seed": "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
    "pub": "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    "msg": "72",
    "sig": "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
           "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
}


@pytest.mark.parametrize("vector", [VECTOR_1, VECTOR_2],
                         ids=["rfc8032-1", "rfc8032-2"])
def test_ed25519_matches_rfc8032_vectors(vector):
    seed = bytes.fromhex(vector["seed"])
    expected_pub = bytes.fromhex(vector["pub"])
    msg = bytes.fromhex(vector["msg"])
    expected_sig = bytes.fromhex(vector["sig"])

    pub, sig = ed25519_sign(seed, msg)
    assert pub == expected_pub
    assert sig == expected_sig
    assert ed25519_verify(pub, sig, msg) is True


def test_ed25519_rejects_tampering_and_malformed():
    pub, sig = ed25519_sign(b"\x01" * 32, b"payload")
    assert ed25519_verify(pub, sig, b"payloadX") is False       # wrong message
    assert ed25519_verify(pub, sig[:-1] + bytes([sig[-1] ^ 1]), b"payload") is False
    assert ed25519_verify(bytes(32), sig, b"payload") is False  # wrong key
    bad_s = sig[:32] + (int.from_bytes(sig[32:], "little") + 1).to_bytes(32, "little")
    assert ed25519_verify(pub, bad_s, b"payload") is False
    assert ed25519_verify(b"short", sig, b"payload") is False   # malformed key
    assert ed25519_verify(pub, sig[:63], b"payload") is False   # short signature


def test_keygen_derives_stable_public_key():
    seed, pub = ed25519_keygen(seed=b"\x42" * 32)
    assert len(seed) == 32 and len(pub) == 32
    assert ed25519_keygen(seed=seed)[1] == pub  # deterministic derivation
    sig = ed25519_sign(seed, b"x")[1]
    assert ed25519_verify(pub, sig, b"x")


# ------------------------------------------------------- signature block rules

def test_signature_block_failure_modes_are_distinguishable():
    seed, pub = ed25519_keygen(seed=b"\x07" * 32)
    payload = {"entries": [1, 2, 3]}
    block = sign_payload(payload, "official", seed)
    keys = {"official": _b64(pub)}

    assert verify_signature_block(block, payload, keys) == "official"

    with pytest.raises(ValueError, match="signature_alg"):
        verify_signature_block({**block, "alg": "rsa"}, payload, keys)
    with pytest.raises(ValueError, match="signature_key"):
        verify_signature_block({**block, "key_id": "who?"}, payload, keys)
    with pytest.raises(ValueError, match="signature_invalid"):
        verify_signature_block(block, {"entries": [3, 2, 1]}, keys)
    with pytest.raises(ValueError, match="must be an object"):
        verify_signature_block("nope", payload, keys)


def test_canonical_json_is_stable_across_key_order():
    assert canonical_json({"a": 1, "b": [2, 3]}) == canonical_json({"b": [2, 3], "a": 1})
    assert canonical_json({"a": 1}) == b'{"a":1}'


# ----------------------------------------------------- signed catalog refresh

def _make_trust():
    seed, pub = ed25519_keygen(seed=b"\x11" * 32)
    keys = {"official-2026": _b64(pub)}
    return seed, keys


def _signed_catalog(seed: str, key_id: str, entries: list[dict]) -> str:
    data = {"entries": entries}
    pub, sig = ed25519_sign(bytes.fromhex(seed),
                            canonical_json(data["entries"]))
    _ = pub
    data["signature"] = {"alg": "ed25519", "key_id": key_id,
                         "signature": _b64(sig)}
    return json.dumps(data, ensure_ascii=False)


async def test_refresh_verifies_signed_catalog_and_reports(tmp_path):
    seed, keys = _make_trust()
    payload = json.dumps({"entries": [
        {"kind": "plugin", "name": "demo", "version": "1.2.0"}]})
    catalog = _signed_catalog(seed.hex(), "official-2026",
                              json.loads(payload)["entries"])

    async def fetch(url):
        return FakeResponse(catalog)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, trusted_keys=keys)
    result = await mgr.refresh()
    assert [e.name for e in result.entries] == ["demo"]
    assert any("官方" not in n and "official-2026" in n for n in result.notes)


async def test_refresh_rejects_tampered_signed_catalog_and_keeps_cache(tmp_path):
    seed, keys = _make_trust()
    entries = [{"kind": "plugin", "name": "demo", "version": "1.2.0"}]
    catalog = _signed_catalog(seed.hex(), "official-2026", entries)
    # attacker flips a field but keeps the publisher's signature
    tampered = json.loads(catalog)
    tampered["entries"][0]["version"] = "9.9.9"

    async def fetch(url):
        return FakeResponse(json.dumps(tampered, ensure_ascii=False))

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, trusted_keys=keys)
    # seed the cache first: a rejected refresh must leave it usable
    mgr._write_cache([CatalogEntry(kind="plugin", name="old", version="1.0.0")],
                     time.time())
    with pytest.raises(UpdateError) as excinfo:
        await mgr.refresh()
    assert excinfo.value.code == "catalog_signature_invalid"
    cached = mgr.list_entries()
    assert [e.name for e in cached.entries] == ["old"]


async def test_refresh_rejects_unknown_signing_key(tmp_path):
    seed, keys = _make_trust()
    entries = [{"kind": "plugin", "name": "demo", "version": "1.2.0"}]
    catalog = _signed_catalog(seed.hex(), "rogue-key", entries)

    async def fetch(url):
        return FakeResponse(catalog)

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, trusted_keys=keys)
    with pytest.raises(UpdateError) as excinfo:
        await mgr.refresh()
    assert excinfo.value.code == "catalog_signature_invalid"


async def test_trusted_keys_default_from_config(tmp_path):
    """cfg.update_trusted_keys is the config.toml [update] trusted_keys table."""

    class KeyedCfg(Cfg):
        update_trusted_keys = {"from-config": "A" * 32}

    seed, _ = _make_trust()
    mgr = UpdateManager(KeyedCfg(), home=tmp_path)
    assert mgr.trusted_keys == KeyedCfg.update_trusted_keys
    mgr2 = UpdateManager(Cfg(), home=tmp_path, trusted_keys={"explicit": "B" * 32})
    assert mgr2.trusted_keys == {"explicit": "B" * 32}  # explicit wins


# --------------------------------------------- artifact signature on install

def _plugin_catalog(sha: str, signature: dict | None = None) -> str:
    entry: dict = {
        "kind": "plugin", "name": "demo", "version": "1.1.0",
        "source_url": "https://cdn.test/demo-1.1.0.py", "sha256": sha,
    }
    if signature is not None:
        entry["signature"] = signature
    return json.dumps({"entries": [entry]})


async def test_apply_verifies_artifact_signature(tmp_path):
    """The chain: unsigned catalog + mirror-controlled checksum, but a
    publisher artifact signature the mirror cannot forge. The mirror swaps
    the bytes AND recomputes sha256 -- the signature still catches it."""
    seed, keys = _make_trust()
    original = b"print('demo v1.1 from the real publisher')\n"
    _, artifact_sig = ed25519_sign(seed, original)
    sig_block = {"alg": "ed25519", "key_id": "official-2026",
                 "signature": _b64(artifact_sig)}

    swapped = b"print('demo -- modified by the mirror')\n"  # attacker's bytes

    async def fetch(url):
        # mirror serves different bytes AND fixes up the (unsigned) catalog's
        # checksum so the sha256 check passes -- only the signature remains
        catalog = _plugin_catalog(hashlib.sha256(swapped).hexdigest(), sig_block)
        if url.endswith("index.json"):
            return FakeResponse(catalog)
        return FakeResponse(swapped.decode("utf-8"))

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, trusted_keys=keys)
    await mgr.refresh()
    result = await mgr.apply(["demo"])
    assert any("artifact_signature_invalid" in e for e in result.errors)
    assert not (tmp_path / "plugins" / "demo.py").exists() \
        if (tmp_path / "plugins").exists() else True


async def test_apply_accepts_validly_signed_artifact(tmp_path):
    seed, keys = _make_trust()
    payload = b"print('demo v1.1 from the real publisher')\n"
    _, artifact_sig = ed25519_sign(seed, payload)
    sig_block = {"alg": "ed25519", "key_id": "official-2026",
                 "signature": _b64(artifact_sig)}

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_plugin_catalog(
                hashlib.sha256(payload).hexdigest(), sig_block))
        return FakeResponse(payload.decode("utf-8"))

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, trusted_keys=keys)
    await mgr.refresh()
    result = await mgr.apply(["demo"])
    assert result.errors == [], result.errors
    assert (tmp_path / "plugins" / "demo.py").read_bytes() == payload


async def test_apply_without_signature_block_stays_checksum_only(tmp_path):
    """Back-compat: unsigned entries keep installing on sha256 match."""
    payload = b"print('demo v1.1')\n"

    async def fetch(url):
        if url.endswith("index.json"):
            return FakeResponse(_plugin_catalog(hashlib.sha256(payload).hexdigest()))
        return FakeResponse(payload.decode("utf-8"))

    mgr = UpdateManager(Cfg(), home=tmp_path, fetch=fetch, trusted_keys={})
    await mgr.refresh()
    result = await mgr.apply(["demo"])
    assert result.errors == [], result.errors


def test_artifact_signature_helper_rejects_tampered_payload():
    seed, pub = ed25519_keygen(seed=b"\x21" * 32)
    _, sig = ed25519_sign(seed, b"real bytes")
    entry = {"signature": {"alg": "ed25519", "key_id": "k",
                           "signature": _b64(sig)}}
    keys = {"k": _b64(pub)}
    assert verify_artifact_signature(entry, b"real bytes", keys)
    with pytest.raises(ValueError, match="signature_invalid"):
        verify_artifact_signature(entry, b"fake bytes", keys)
