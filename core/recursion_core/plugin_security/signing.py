"""Ed25519 verification through the ``cryptography`` package (pyca), never hand-rolled.

Verification only: the core never holds a signing key. Developer keys sign plugin
manifests on the developer's machine; the installer key signs authority profiles on the
user's machine. Keys and signatures travel as lowercase hex.

If ``cryptography`` cannot be imported, every verification refuses
(``signature_backend_unavailable``). Nothing falls back to "valid". Note for the 3.14t
build: ``cryptography`` is a compiled extension, so whether the installed wheel declares
free-threading support must be checked on the target interpreter (the loader's GIL check
does not cover the core's own imports). On this box 3.14t has no ``cryptography``
installed; that is reported, not worked around.
"""

from __future__ import annotations

import hashlib
import re

from .errors import Refused

__all__ = ["backend_available", "key_id", "parse_public_key", "verify"]

try:  # pragma: no cover - depends on the interpreter
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # pragma: no cover
    Ed25519PublicKey = None  # type: ignore[assignment]
    InvalidSignature = Exception  # type: ignore[assignment,misc]

_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX128 = re.compile(r"[0-9a-f]{128}")


def backend_available() -> bool:
    return Ed25519PublicKey is not None


def parse_public_key(hex_key: object) -> bytes:
    """Return the 32 raw key bytes, or refuse a malformed key."""
    if type(hex_key) is not str or not _HEX64.fullmatch(hex_key):
        raise Refused("malformed_public_key")
    return bytes.fromhex(hex_key)


def key_id(hex_key: str) -> str:
    """Stable identifier of a public key: ``ed25519:`` plus the first 32 hex of its SHA-256."""
    return "ed25519:" + hashlib.sha256(parse_public_key(hex_key)).hexdigest()[:32]


def verify(hex_key: str, hex_signature: object, message: bytes) -> bool:
    """True only for a well-formed signature that verifies; False for every bad signature.

    A missing backend refuses instead of answering, because "unknown" must not read as
    either valid or merely unsigned.
    """
    raw_key = parse_public_key(hex_key)
    if type(hex_signature) is not str or not _HEX128.fullmatch(hex_signature):
        return False
    if Ed25519PublicKey is None:
        raise Refused("signature_backend_unavailable")
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(bytes.fromhex(hex_signature), message)
    except (InvalidSignature, ValueError):
        return False
    return True
