"""Restricted canonical JSON for digests and signatures (candidate form ``csl-cj/v0``).

The approval digest, the plugin manifest signature and the authority profile signature
are all taken over these bytes, so a later Rust core must produce the same bytes from
the same value. The form is deliberately narrower than RFC 8785 so the two coincide on
everything it accepts (O49 is still open; this is not a decision on the digest form):

* objects with ASCII string keys, sorted by code point, no duplicate keys;
* arrays, ASCII strings, ``true``, ``false``, ``null``;
* integers in ``[-(2**53 - 1), 2**53 - 1]``. No floats, NaN, infinities or exponents,
  so money is always integer minor units;
* no whitespace; ``bool`` is never accepted where an integer is expected.

``parse`` is the strict reader for bytes that arrive over the protocol: it refuses
duplicate keys, floats, non-finite numbers, out-of-range integers, non-ASCII text and
any input that does not round-trip to the identical bytes.
"""

from __future__ import annotations

import json

from .errors import Refused

__all__ = ["MAX_INT", "canonical", "parse", "check_value"]

MAX_INT = 2**53 - 1
_MAX_DEPTH = 32


def check_value(value: object, depth: int = 0) -> None:
    """Raise ``Refused('noncanonical_value')`` unless ``value`` fits the form."""
    if depth > _MAX_DEPTH:
        raise Refused("noncanonical_value", "too deep")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if -MAX_INT <= value <= MAX_INT:
            return
        raise Refused("noncanonical_value", "integer out of range")
    if type(value) is str:
        if value.isascii():
            return
        raise Refused("noncanonical_value", "non-ASCII text")
    if type(value) in (list, tuple):
        for item in value:
            check_value(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise Refused("noncanonical_value", "key")
            check_value(item, depth + 1)
        return
    raise Refused("noncanonical_value", type(value).__name__)


def canonical(value: object) -> bytes:
    """Return the canonical bytes of ``value`` or refuse."""
    check_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, item in pairs:
        if key in out:
            raise Refused("noncanonical_value", "duplicate key")
        out[key] = item
    return out


def _no_float(text: str) -> object:
    raise Refused("noncanonical_value", "float")


def _no_constant(text: str) -> object:
    raise Refused("noncanonical_value", "non-finite")


def parse(raw: bytes) -> object:
    """Strictly parse canonical bytes; refuse anything that is not exactly canonical."""
    if type(raw) is not bytes:
        raise Refused("noncanonical_value", "not bytes")
    try:
        text = raw.decode("ascii")
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_float=_no_float,
                           parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, Refused):
            raise
        raise Refused("noncanonical_value", "not JSON") from None
    if canonical(value) != raw:
        raise Refused("noncanonical_value", "not in canonical form")
    return value
