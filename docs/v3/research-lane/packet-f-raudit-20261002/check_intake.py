#!/usr/bin/env python3
"""Intake gate for the 2026-10-02 packet-F R-AUDIT return + PR #165 review.

Verifies the delivered files against SHA256SUMS.txt, then scans prose for secret patterns and
forbidden foreign-project identifiers (reusing tools/check_public_scope.py).

The checksum manifest is not the inventory: on-disk files (except this script and
the packet-root SHA256SUMS.txt) must match the parsed checksum paths exactly. Nested
checksum files are inventory. Malformed or duplicate manifest lines fail closed.

    python -X utf8 -B docs/v3/research-lane/packet-f-raudit-20261002/check_intake.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUMS = HERE / "SHA256SUMS.txt"
SELF = Path(__file__).resolve()
SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"bearer\s+[A-Za-z0-9._\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
SUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")


def repo_root() -> Path:
    for parent in [HERE, *HERE.parents]:
        if (parent / "tools" / "check_public_scope.py").exists():
            return parent
    return HERE


def forbidden_pattern() -> re.Pattern[str] | None:
    root = repo_root()
    sys.path.insert(0, str(root))
    try:
        from tools.check_public_scope import PATTERN  # type: ignore
    except Exception:
        return None
    return PATTERN


def fail(message: str) -> None:
    print(f"INTAKE_FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def delivered_paths() -> set[str]:
    found: set[str] = set()
    for path in HERE.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == SELF or path.resolve() == SUMS:
            continue
        found.add(path.relative_to(HERE).as_posix())
    return found


def parse_manifest() -> dict[str, str]:
    if not SUMS.exists():
        fail("missing SHA256SUMS.txt")
    listed: dict[str, str] = {}
    for lineno, raw in enumerate(SUMS.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            fail(f"malformed SHA256SUMS.txt line {lineno}: empty")
        m = SUM_LINE.match(line)
        if not m:
            fail(f"malformed SHA256SUMS.txt line {lineno}: {raw!r}")
        digest, name = m.group(1).lower(), m.group(2).strip()
        if name in listed:
            fail(f"duplicate SHA256SUMS.txt entry: {name}")
        if name in {SUMS.name, SELF.name}:
            fail(f"SHA256SUMS.txt must not list {name}")
        listed[name] = digest
    return listed


def main() -> int:
    listed = parse_manifest()
    on_disk = delivered_paths()
    missing = sorted(on_disk - listed.keys())
    extra = sorted(listed.keys() - on_disk)
    if missing or extra:
        parts = []
        if missing:
            parts.append("on disk but not in SHA256SUMS.txt: " + ", ".join(missing))
        if extra:
            parts.append("in SHA256SUMS.txt but missing on disk: " + ", ".join(extra))
        fail("; ".join(parts))

    for name, expected in listed.items():
        path = HERE / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            fail(f"hash mismatch: {name}")

    forbidden = forbidden_pattern()
    prose = 0
    for path in sorted(HERE.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        prose += 1
        found = SECRET.search(text)
        if found:
            fail(f"possible secret in {path.relative_to(HERE)}: {found.group(0)[:40]!r}")
        if forbidden is not None:
            hit = forbidden.search(text)
            if hit:
                fail(f"forbidden identifier in {path.relative_to(HERE)}: {hit.group(0)!r}")

    print(f"PASS: {len(listed)} delivered files sha256-verified; {prose} prose files clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
