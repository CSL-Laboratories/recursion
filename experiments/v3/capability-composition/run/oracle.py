"""Independent oracle for the R-PC literal specimen.

Computes the expected output and byte ranges **independently** of ``reference_model``: it consumes
plain ``(id, data, order)`` tuples and uses its own join/cursor logic. The gate verifies this module
does not import the candidate model, so agreement is meaningful evidence rather than a tautology.

The oracle must never be told the candidate's answer; it derives the expectation from the frozen
input specification only.
"""
from __future__ import annotations

import json


def expected(items: list[tuple[str, bytes, int]]) -> tuple[bytes, dict[str, tuple[int, int]]]:
    ordered = sorted(items, key=lambda t: (t[2], t[0]))
    parts = [data for _, data, _ in ordered]
    output = b"".join(parts)

    ranges: dict[str, tuple[int, int]] = {}
    cursor = 0
    for sid, data, _ in ordered:
        length = len(data)
        ranges[sid] = (cursor, cursor + length)
        cursor = cursor + length

    assert cursor == len(output)
    return output, ranges


def expected_ranges_by_codepoint(items: list[tuple[str, bytes, int]]) -> dict[str, tuple[int, int]]:
    """A deliberately wrong mutant oracle: counts Unicode codepoints, not UTF-8 bytes."""
    ordered = sorted(items, key=lambda t: (t[2], t[0]))
    ranges: dict[str, tuple[int, int]] = {}
    cursor = 0
    for sid, data, _ in ordered:
        length = len(data.decode("utf-8"))
        ranges[sid] = (cursor, cursor + length)
        cursor = cursor + length
    return ranges


def valid_artifact(payload: bytes) -> bool:
    """Independent acceptance predicate for F16 (non-empty, bounded, newline-terminated, ASCII)."""
    return 0 < len(payload) <= 64 and payload.endswith(b"\n") and all(b < 128 for b in payload)


def expected_manifest(items: list[tuple[str, bytes, int]]) -> bytes:
    """Independent canonical-manifest construction for F01/F11/F12 (own assembly)."""
    output, ranges = expected(items)
    obj = {"profile": "rpc-canonical:v1",
           "ranges": {k: list(v) for k, v in sorted(ranges.items())}, "total": len(output)}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def expected_identity(input_bytes: bytes, output_bytes: bytes, profile: str = "rpc-identity:v1") -> bytes:
    """Independent length-prefixed identity encoding for F02 (Q09)."""
    import hashlib
    fields = (b"profile", profile.encode(), b"input", input_bytes, b"output", output_bytes)
    encoded = b"".join(len(f).to_bytes(8, "big") + f for f in fields)
    return hashlib.sha256(encoded).digest()
