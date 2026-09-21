"""Supply-chain hardening (SUPPLY-01): publisher signatures + checksum chain.

Ed25519 per RFC 8032, pure Python, verification-first. The project avoids
third-party crypto on the happy path; verify correctness is pinned by the
official RFC 8032 test vectors (tests/test_supply_chain.py). Sign/verify on
tiny inputs (catalog manifests, package digests) costs a few milliseconds.

Trust model — the checksum chain:

    publisher key (pinned in [update] trusted_keys / passed explicitly)
      └─ signs the canonical catalog `entries` list        (catalog signature)
           └─ each entry carries `sha256` of its artifact   (checksum)
                └─ optional per-entry `signature` block     (artifact signature,
                   signs the exact downloaded bytes — protects against a
                   mirror serving a different file with a *stale* checksum
                   line, and against checksum-only catalogs)

Catalog signature block (top level, next to "entries"):

    "signature": {"alg": "ed25519", "key_id": "official-2026",
                  "signature": "<base64 of 64 raw bytes>"}

Per-entry artifact signature block (inside a CatalogEntry dict): same shape.
A missing block never fails verification — an unsigned catalog is *visible*
as unsigned (UpdateResult carries it), never masquerades as verified.

CLI (publisher side):
    python -m oaset.supply keygen
    python -m oaset.supply sign-catalog catalog.json --seed <hex>
    python -m oaset.supply verify catalog.json --key <hex>
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Ed25519 — RFC 8032 (pure Python, extended coordinates)
# --------------------------------------------------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)

_IDENTITY = (0, 1, 1, 0)  # neutral element, extended coords (X, Y, Z, T)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _xrecover(y: int) -> int:
    xx = ((y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if (x * x - xx) % _P != 0:
        raise ValueError("point not on curve: no square root")
    if x % 2 != 0:
        x = _P - x
    return x


_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = _xrecover(_BY)
_B = (_BX, _BY, 1, (_BX * _BY) % _P)  # base point


def _edwards_add(p: tuple[int, int, int, int],
                 q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * _D * t1 * t2 % _P
    d = 2 * z1 * z2 % _P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _scalarmult(p: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    result = _IDENTITY
    addend = p
    while e > 0:
        if e & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        e >>= 1
    return result


def _isoncurve(p: tuple[int, int, int, int]) -> bool:
    x, y, z, t = p
    # a = -1 Twisted Edwards: y^2 = x^2 + z^2 + d*t^2 (z-normalised), plus
    # the extended-coordinate consistency relation t = x*y/z
    return (z % _P != 0 and x * y % _P == z * t % _P
            and (y * y - x * x - z * z - _D * t * t) % _P == 0)


def _encodepoint(p: tuple[int, int, int, int]) -> bytes:
    x, y, z, _t = p
    zi = pow(z, _P - 2, _P)
    x = x * zi % _P
    y = y * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decodepoint(data: bytes) -> tuple[int, int, int, int]:
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if x & 1 != (data[31] >> 7) & 1:
        x = _P - x
    p = (x, y, 1, x * y % _P)
    if not _isoncurve(p):
        raise ValueError("point not on curve")
    return p


def _secret_clamp(h32: bytes) -> int:
    a = int.from_bytes(h32, "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a


def ed25519_keygen(seed: bytes | None = None) -> tuple[bytes, bytes]:
    """(seed, public_key) — 32 raw bytes each. The seed IS the secret."""
    seed = seed or secrets.token_bytes(32)
    a = _secret_clamp(_sha512(seed)[:32])
    return seed, _encodepoint(_scalarmult(_B, a))


def ed25519_sign(seed: bytes, msg: bytes) -> tuple[bytes, bytes]:
    """(public_key, signature) — RFC 8032 §5.1."""
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed)
    a = _secret_clamp(h[:32])
    pub = _encodepoint(_scalarmult(_B, a))
    r = int.from_bytes(_sha512(h[32:] + msg), "little") % _L
    enc_r = _encodepoint(_scalarmult(_B, r))
    k = int.from_bytes(_sha512(enc_r + pub + msg), "little") % _L
    s = (r + k * a) % _L
    return pub, enc_r + int.to_bytes(s, 32, "little")


def ed25519_verify(public_key: bytes, signature: bytes, msg: bytes) -> bool:
    """RFC 8032 verification; returns False on ANY malformed input."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        a = _decodepoint(public_key)
        r = _decodepoint(signature[:32])
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        k = int.from_bytes(_sha512(signature[:32] + public_key + msg), "little") % _L
        left = _scalarmult(_B, s)
        right = _edwards_add(r, _scalarmult(a, k))
        return _encodepoint(left) == _encodepoint(right)
    except (ValueError, IndexError):
        return False


# --------------------------------------------------------------------------
# Canonical serialization + signature blocks
# --------------------------------------------------------------------------

SUPPORTED_ALG = "ed25519"


def canonical_json(obj: Any) -> bytes:
    """Stable serialization: the exact bytes a publisher signed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _b64decode_strict(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def sign_payload(payload: Any, key_id: str, seed: bytes) -> dict[str, str]:
    """Build a signature block over any JSON-able payload. Signature blocks
    reference the verifying key by id — the public key lives in the trust
    store, never inside the signed artifact."""
    _pub, sig = ed25519_sign(seed, canonical_json(payload))
    return {"alg": SUPPORTED_ALG, "key_id": key_id,
            "signature": base64.b64encode(sig).decode("ascii")}


def verify_signature_block(block: Any, payload: Any,
                           trusted_keys: dict[str, str]) -> str:
    """Verify one signature block over the CANONICAL JSON of `payload`.

    Returns the key_id that verified. Raises ValueError with a
    machine-readable prefix on every failure mode (alg/key/shape/signature)
    so callers can surface structured errors instead of guessing.
    """
    return _verify_block(block, canonical_json(payload), trusted_keys)


def _verify_block(block: Any, message: bytes,
                  trusted_keys: dict[str, str]) -> str:
    if not isinstance(block, dict):
        raise ValueError("signature_block: signature block must be an object")
    alg = block.get("alg")
    if alg != SUPPORTED_ALG:
        raise ValueError(f"signature_alg: unsupported algorithm {alg!r}")
    key_id = str(block.get("key_id", ""))
    key_b64 = trusted_keys.get(key_id)
    if key_b64 is None:
        raise ValueError(f"signature_key: key_id {key_id!r} is not trusted")
    try:
        pub = _b64decode_strict(key_b64)
        sig = _b64decode_strict(str(block.get("signature", "")))
    except Exception as exc:
        raise ValueError(f"signature_key: malformed key/signature encoding ({exc})") from exc
    if len(pub) != 32:
        raise ValueError("signature_key: trusted key must be 32 raw bytes")
    if not ed25519_verify(pub, sig, message):
        raise ValueError("signature_invalid: signature does not match payload")
    return key_id


def catalog_signature_block(catalog: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the top-level signature block, if the catalog carries one."""
    block = catalog.get("signature")
    return block if isinstance(block, dict) else None


def verify_catalog_signature(catalog: dict[str, Any],
                             trusted_keys: dict[str, str]) -> str:
    """Verify the catalog's top-level signature over its `entries` list."""
    return verify_signature_block(catalog_signature_block(catalog),
                                  catalog.get("entries"), trusted_keys)


def verify_artifact_signature(entry: Any, payload: bytes,
                              trusted_keys: dict[str, str]) -> str:
    """Verify a per-entry artifact signature over the EXACT downloaded bytes
    (no JSON canonicalization — the artifact is opaque binary).

    `entry` is a CatalogEntry (its `signature` field) or a raw dict.
    """
    block = entry.get("signature") if isinstance(entry, dict) \
        else getattr(entry, "signature", None)
    return _verify_block(block, payload, trusted_keys)


def fingerprint(data: bytes) -> str:
    """sha256 hex digest — the checksum chain's bottom link."""
    return hashlib.sha256(data).hexdigest()


def trusted_key_report(trusted_keys: dict[str, str]) -> Iterable[str]:
    """Human-readable lines for diagnostics (never leaks private material —
    trusted keys ARE public keys)."""
    for key_id, key_b64 in sorted(trusted_keys.items()):
        yield f"{key_id}: {key_b64}"


if __name__ == "__main__":  # pragma: no cover - publisher CLI
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m oaset.supply",
        description="Ed25519 keygen / catalog signing / offline verification")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen", help="print a fresh seed (secret) and public key")
    p_sign = sub.add_parser("sign-catalog", help="sign a catalog file in place")
    p_sign.add_argument("path")
    p_sign.add_argument("--seed", required=True, help="secret seed, 64 hex chars")
    p_sign.add_argument("--key-id", required=True)
    p_verify = sub.add_parser("verify", help="verify a signed catalog offline")
    p_verify.add_argument("path")
    p_verify.add_argument("--key", required=True, help="public key, 64 hex chars")
    p_verify.add_argument("--key-id", default="provided")
    args = parser.parse_args()

    if args.cmd == "keygen":
        seed, pub = ed25519_keygen()
        print(f"seed (SECRET, keep offline): {seed.hex()}")
        print(f"public key (trusted_keys):   {pub.hex()}")
        print(f"public key (base64):         {base64.b64encode(pub).decode()}")
    elif args.cmd == "sign-catalog":
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        block = sign_payload(data.get("entries"), args.key_id,
                             bytes.fromhex(args.seed))
        data["signature"] = block
        Path(args.path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"signed with key {args.key_id}")
    elif args.cmd == "verify":
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
        pub_hex = args.key.replace(":", "")
        try:
            verify_signature_block(
                catalog_signature_block(data), data.get("entries"),
                {args.key_id: base64.b64encode(bytes.fromhex(pub_hex)).decode()})
        except ValueError as exc:
            print(f"INVALID: {exc}", file=sys.stderr)
            sys.exit(1)
        print("OK")
