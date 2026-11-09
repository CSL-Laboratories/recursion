"""CSL canonical form v1 (``csl-canonical/1``): strict parse, canonical bytes, SHA-256.

A restricted RFC 8785 (JCS) profile, ported from the #1100 contract's reference
implementation (csl-research PR #1100 at 114b48be,
``contracts/recursion-cockpit/v1.1-proposed/csl_contract.py`` and ``CANONICAL-FORM.md``).
Every in-domain value serialises to exactly the bytes RFC 8785 gives for it. The domain is
narrowed so no implementation has to reproduce ECMAScript number formatting:

* JSON text in UTF-8 with no byte order mark;
* duplicate object keys refused at any depth, compared after unescaping (never last-wins);
* integers only, in -(2^53-1) .. 2^53-1; fractions, exponents (even integral ``1.0`` or
  ``1e2``), NaN and the infinities are refused;
* strings must be Unicode scalar values (a lone surrogate is refused); no normalisation;
* nesting deeper than 128 is refused.

Standard library only. Anything outside the domain raises :class:`CanonicalError`, whose
``kind`` names the rule (the contract's refusal code is ``canonical_form_invalid``).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CANONICAL_FORM = "csl-canonical/1"
MAX_SAFE = 2**53 - 1
MAX_DEPTH = 128
REFUSAL_CODE = "canonical_form_invalid"

# Refusal kinds, as recorded in the vector files.
KINDS = frozenset({
    "bom", "not_utf8", "not_json", "duplicate_key", "float", "non_finite", "integer_range",
    "lone_surrogate", "depth", "non_string_key", "unsupported_type",
})


class CanonicalError(ValueError):
    """Input outside the CSL canonical form v1 domain (refusal ``canonical_form_invalid``)."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.code = REFUSAL_CODE


def _no_float(text: str) -> Any:
    raise CanonicalError("float", repr(text))


def _no_constant(text: str) -> Any:
    raise CanonicalError("non_finite", repr(text))


def _int(text: str) -> int:
    value = int(text)
    if not -MAX_SAFE <= value <= MAX_SAFE:
        raise CanonicalError("integer_range", text[:40])
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CanonicalError("duplicate_key", repr(key))
        out[key] = value
    return out


def loads_strict(data: str | bytes) -> Any:
    """Parse JSON text in the canonical-form domain, or raise :class:`CanonicalError`."""
    if isinstance(data, bytes):
        if data.startswith(b"\xef\xbb\xbf"):
            raise CanonicalError("bom")
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise CanonicalError("not_utf8", e.reason) from None
    elif data.startswith("﻿"):
        raise CanonicalError("bom")
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_float=_no_float, parse_int=_int,
                           parse_constant=_no_constant)
    except CanonicalError:
        raise
    except (ValueError, RecursionError) as e:
        raise CanonicalError("not_json", str(e)) from None
    check_value(value)
    return value


def check_value(value: Any, depth: int = 0) -> None:
    """Raise :class:`CanonicalError` if an in-memory value is outside the domain."""
    if depth > MAX_DEPTH:
        raise CanonicalError("depth", f"nesting deeper than {MAX_DEPTH}")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise CanonicalError("lone_surrogate") from None
    elif isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int):
        if not -MAX_SAFE <= value <= MAX_SAFE:
            raise CanonicalError("integer_range")
    elif isinstance(value, float):
        raise CanonicalError("non_finite" if value != value or value in (float("inf"), float("-inf"))
                             else "float")
    elif isinstance(value, list):
        for item in value:
            check_value(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError("non_string_key")
            check_value(key, depth + 1)
            check_value(item, depth + 1)
    else:
        raise CanonicalError("unsupported_type", type(value).__name__)


_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


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


def _utf16_key(s: str) -> bytes:
    # RFC 8785 3.2.3: members sort by UTF-16 code units, not code points.
    return s.encode("utf-16-be", "surrogatepass")


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    else:
        out.append("{")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")


def canonical_bytes(value: Any) -> bytes:
    """Canonical UTF-8 bytes of an in-domain value (RFC 8785 output for this profile)."""
    check_value(value)
    out: list[str] = []
    _serialize(value, out)
    return "".join(out).encode("utf-8")


def digest(value: Any) -> str:
    """SHA-256 of the canonical bytes, as 64 lowercase hex characters."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_text(data: str | bytes) -> str:
    return digest(loads_strict(data))


_HEX64 = re.compile(r"[0-9a-f]{64}")


def is_full_digest(s: Any) -> bool:
    """A digest is 64 lowercase hex characters: no prefix, no uppercase, no truncation."""
    return isinstance(s, str) and _HEX64.fullmatch(s) is not None
