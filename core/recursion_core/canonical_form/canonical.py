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
    # 2^53-1 is 16 digits. Longer tokens are out of range; reject them before
    # int() so CPython's integer-string limit (4300 digits by default) cannot
    # turn them into ValueError and then not_json.
    digits = text[1:] if text.startswith("-") else text
    if len(digits) > 16:
        raise CanonicalError("integer_range", text[:40])
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
    """Parse JSON text in the canonical-form domain, or raise :class:`CanonicalError`.

    A value that is not ``str`` or ``bytes`` is ``not_json``. Nesting the host
    decoder cannot walk is ``depth``, the same rule as a value deeper than
    :data:`MAX_DEPTH`.
    """
    if isinstance(data, bytes):
        if data.startswith(b"\xef\xbb\xbf"):
            raise CanonicalError("bom")
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise CanonicalError("not_utf8", e.reason) from None
    elif isinstance(data, str):
        if data.startswith("\ufeff"):
            raise CanonicalError("bom")
    else:
        raise CanonicalError("not_json", type(data).__name__)
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_float=_no_float, parse_int=_int,
                           parse_constant=_no_constant)
    except CanonicalError:
        raise
    except RecursionError:
        raise CanonicalError("depth", f"nesting deeper than {MAX_DEPTH}") from None
    except ValueError as e:
        raise CanonicalError("not_json", str(e)) from None
    check_value(value)
    return value


def check_value(value: Any, depth: int = 0) -> None:
    """Raise :class:`CanonicalError` if an in-memory value is outside the domain.

    The walk is an explicit stack. A recursive walk raises ``RecursionError``
    for an in-domain value when the caller is already near the recursion limit,
    and for a too-deep value it leaks that error instead of ``depth``.
    """
    # ("check", value, depth) or ("dict", items, next_index, depth).
    # Dict pairs run left to right: the key is fully checked, then its value,
    # and a non-string key is reported when that pair is reached, not before
    # earlier pairs.
    stack: list[tuple] = [("check", value, depth)]
    while stack:
        frame = stack.pop()
        if frame[0] == "dict":
            _, items, index, depth = frame
            if index >= len(items):
                continue
            key, item = items[index]
            if not isinstance(key, str):
                raise CanonicalError("non_string_key")
            stack.append(("dict", items, index + 1, depth))
            stack.append(("check", item, depth + 1))
            stack.append(("check", key, depth + 1))
            continue
        _, current, depth = frame
        if depth > MAX_DEPTH:
            raise CanonicalError("depth", f"nesting deeper than {MAX_DEPTH}")
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeEncodeError:
                raise CanonicalError("lone_surrogate") from None
        elif isinstance(current, bool) or current is None:
            pass
        elif isinstance(current, int):
            if not -MAX_SAFE <= current <= MAX_SAFE:
                raise CanonicalError("integer_range")
        elif isinstance(current, float):
            raise CanonicalError("non_finite" if current != current or current in (float("inf"), float("-inf"))
                                 else "float")
        elif isinstance(current, list):
            for item in reversed(current):
                stack.append(("check", item, depth + 1))
        elif isinstance(current, dict):
            stack.append(("dict", list(current.items()), 0, depth))
        else:
            raise CanonicalError("unsupported_type", type(current).__name__)


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
    """Append canonical JSON for ``value``. Iterative, same bytes as the recursive form.

    ``check_value`` has already rejected anything that is not null, a boolean,
    an in-range int, a string, a list, or a dict with string keys.
    """
    stack: list[tuple] = [("val", value)]
    while stack:
        kind, current = stack.pop()
        if kind == "lit":
            out.append(current)
            continue
        if current is None:
            out.append("null")
        elif current is True:
            out.append("true")
        elif current is False:
            out.append("false")
        elif isinstance(current, int):
            out.append(str(current))
        elif isinstance(current, str):
            out.append(_string(current))
        elif isinstance(current, list):
            stack.append(("lit", "]"))
            for index in range(len(current) - 1, -1, -1):
                stack.append(("val", current[index]))
                if index:
                    stack.append(("lit", ","))
            stack.append(("lit", "["))
        else:
            keys = sorted(current, key=_utf16_key)
            stack.append(("lit", "}"))
            for index in range(len(keys) - 1, -1, -1):
                key = keys[index]
                stack.append(("val", current[key]))
                stack.append(("lit", ":"))
                stack.append(("lit", _string(key)))
                if index:
                    stack.append(("lit", ","))
            stack.append(("lit", "{"))


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
