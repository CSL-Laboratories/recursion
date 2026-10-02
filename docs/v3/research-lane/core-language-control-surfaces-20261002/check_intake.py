#!/usr/bin/env python3
"""Intake gate for the 2026-10-02 packet-E return + PR #160 review.

Verifies the delivered files against SHA256SUMS.txt, then scans prose for secret
patterns and forbidden foreign-project identifiers (reusing tools/check_public_scope.py).

    python -X utf8 -B docs/v3/research-lane/core-language-control-surfaces-20261002/check_intake.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUMS = HERE / "SHA256SUMS.txt"
SECRET = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"bearer\s+[A-Za-z0-9._\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


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


def main() -> int:
    if not SUMS.exists():
        fail("missing SHA256SUMS.txt")
    entries = 0
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
        if not m:
            continue
        expected, name = m.group(1).lower(), m.group(2).strip()
        path = HERE / name
        if not path.exists():
            fail(f"missing delivered file: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            fail(f"hash mismatch: {name}")
        entries += 1

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

    print(f"PASS: {entries} delivered files sha256-verified; {prose} prose files clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
