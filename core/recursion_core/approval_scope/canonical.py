"""Restricted canonical JSON for the approval digest (candidate form ``csl-cj/v0``).

Mirrors ``plugin_security/canonical.py`` (recursion #328) so both packages hash the same
bytes for the same value; O49 (the final digest form) is still open. Accepted values:

* objects with ASCII string keys (sorted, no duplicates), arrays, ASCII strings,
  ``true``, ``false``, ``null``;
* integers in ``[-(2**53 - 1), 2**53 - 1]``; no floats, NaN or infinities;
* ``bool`` is never accepted where an integer is expected.
"""

from __future__ import annotations

import hashlib
import json

from .errors import Refused

__all__ = ["MAX_INT", "canonical", "check_value", "digest"]

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


def digest(value: object) -> str:
    """SHA-256 hex digest of the canonical bytes of ``value``."""
    return hashlib.sha256(canonical(value)).hexdigest()
