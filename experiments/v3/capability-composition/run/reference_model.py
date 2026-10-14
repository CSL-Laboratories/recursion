"""Reference model (candidate under test) for the R-PC literal specimen.

Minimal provider-free model of the deterministic assembler used by F01/F11/F12:

- sources are an unordered SET; the render order comes from a pinned declared ``order`` key and a
  stable tie-break on ``id`` — **never** from enumeration order, filesystem order or arrival order;
- ranges are half-open **UTF-8 byte** offsets;
- the canonical manifest is deterministic JSON (sorted keys, no whitespace).

This model is the *candidate*; the expected values live in the independent ``oracle`` module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    id: str
    data: bytes
    order: int


def render(sources: list[Source]) -> tuple[bytes, dict[str, tuple[int, int]], list[tuple[str, int, int]]]:
    ordered = sorted(sources, key=lambda s: (s.order, s.id))
    out = bytearray()
    ranges: dict[str, tuple[int, int]] = {}
    spans: list[tuple[str, int, int]] = []
    for source in ordered:
        start = len(out)
        out += source.data
        end = len(out)
        ranges[source.id] = (start, end)
        spans.append((source.id, start, end))
    return bytes(out), ranges, spans


def canonical_manifest(ranges: dict[str, tuple[int, int]], total: int) -> bytes:
    obj = {"profile": "rpc-canonical:v1",
           "ranges": {k: [v[0], v[1]] for k, v in sorted(ranges.items())}, "total": total}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def prefix_plan(output: bytes, n: int = 18) -> bytes:
    return output[:n]


def verify_spans(spans: list[tuple[str, int, int]], total: int) -> dict:
    if not spans:
        return {"outcome": "refuse", "reason": "empty_span_map"}
    ordered = sorted(spans, key=lambda s: (s[1], s[2]))
    prev_end = 0
    for _sid, start, end in ordered:
        if not (0 <= start <= end <= total):
            return {"outcome": "refuse", "reason": "invalid_coordinate"}
        if start != prev_end:
            return {"outcome": "refuse", "reason": "uncovered_output_byte"}
        prev_end = end
    if prev_end != total:
        return {"outcome": "refuse", "reason": "uncovered_output_byte"}
    return {"outcome": "ok"}


def render_by_enumeration(sources: list[Source]) -> bytes:
    """A deliberately wrong mutant: renders in list/enumeration order."""
    out = bytearray()
    for source in sources:
        out += source.data
    return bytes(out)


def render_empty_spans(sources: list[Source]):
    out, ranges, _ = render(sources)
    return out, ranges, []


def render_invalid_coordinate(sources: list[Source]):
    out, ranges, spans = render(sources)
    spans = list(spans)
    sid, start, _end = spans[-1]
    spans[-1] = (sid, start, len(out) + 1)
    return out, ranges, spans


def render_gap_spans(sources: list[Source]):
    out, ranges, spans = render(sources)
    return out, ranges, list(spans[:-1])


def render_corrupt_core(sources: list[Source]):
    """Keeps length/ranges but corrupts the core bytes (detected by exact-byte assertions)."""
    out, ranges, spans = render(sources)
    corrupted = bytearray(out)
    start, end = ranges.get("core", (0, 0))
    for i in range(start, end):
        corrupted[i] = ord("X")
    return bytes(corrupted), ranges, spans
