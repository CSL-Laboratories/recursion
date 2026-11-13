r"""Canonical JSON for the approval digest: the ASCII subset of ``csl-cj/v0`` serialised
exactly as ``csl-canonical/1`` (recursion #339, ``core/recursion_core/canonical_form``).

The bytes are the ones #339's ``canonical_bytes`` gives for the same value, so an approval
digest and a #339 digest over the same effect agree (PRODUCT.md 4.2; O49, the final digest
form, is still open). String escaping follows RFC 8785 / #339: ``"`` and ``\`` are
backslash-escaped, ``\b \t \n \f \r`` use their short forms, other characters below
U+0020 are ``\u00xx``, and everything else, DEL (U+007F) included, is written as itself.
Members sort by UTF-16 code units (for ASCII keys, the same as sorting by code point).
Earlier heads used ``json.dumps(ensure_ascii=True)``, which writes DEL as ``\u007f`` and so
gave a different digest than #339 for any string holding DEL. Accepted values:

* objects with ASCII string keys (sorted, no duplicates), arrays, ASCII strings,
  ``true``, ``false``, ``null``;
* integers in ``[-(2**53 - 1), 2**53 - 1]``; no floats, NaN or infinities;
* ``bool`` is never accepted where an integer is expected.
"""

from __future__ import annotations

import hashlib

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


# Same table and rule as #339 ``canonical_form/canonical.py`` ``_string``.
_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f",
        "\r": "\\r"}


def _string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _serialize(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif type(value) is int:
        out.append(str(value))
    elif type(value) is str:
        out.append(_string(value))
    elif type(value) in (list, tuple):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    else:
        out.append("{")
        for i, key in enumerate(sorted(value, key=lambda k: k.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")


def canonical(value: object) -> bytes:
    """Return the canonical bytes of ``value`` or refuse."""
    check_value(value)
    out: list[str] = []
    _serialize(value, out)
    return "".join(out).encode("ascii")


def digest(value: object) -> str:
    """SHA-256 hex digest of the canonical bytes of ``value``."""
    return hashlib.sha256(canonical(value)).hexdigest()
