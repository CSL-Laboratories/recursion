#!/usr/bin/env python3
"""Public-scope gate: Recursion's public repository must not embed other projects.

Scans text files for identifiers that belong to repositories with their own lane, plus the
physics/prototype track. Deterministic and provider-free. Run from the repository root:

    python -X utf8 -B tools/check_public_scope.py

A pass certifies only that no known foreign identifier remains in tracked text. It does not
certify git history, which is a separate publication decision.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

FORBIDDEN = [
    r"golden[- ]stallion",
    r"\bchiron\b",
    r"cubic[- ]castles",
    r"csl[- ]labs",
    r"\bGSC\b",
    r"\bphysics\b",
    r"syllabus",
    r"ai-investment-cockpit",
    r"\binvestment[- ]cockpit\b",
    r"\bcockpit\b",
    r"edgar-morning-brew",
    r"guerra-construction",
    r"santa-clara",
]
PATTERN = re.compile("|".join(FORBIDDEN), re.IGNORECASE)

SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".codex-tmp", "node_modules"}
# Paths excluded from the published tree (see PUBLIC-RELEASE.md); they are private or
# immutable historical artifacts and are not part of the public release.
EXCLUDE_PREFIXES = (
    "docs/v3/archive/",
    "docs/audits/recursion-v2-phase0-transition-receipts/",
)
SKIP_SUFFIX = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".zip",
}


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(EXCLUDE_PREFIXES):
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIX:
            continue
        yield path


def main() -> int:
    violations: list[str] = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = PATTERN.search(line)
            if match:
                rel = path.relative_to(ROOT)
                violations.append(f"{rel}:{number}: '{match.group(0)}': {line.strip()[:100]}")

    if violations:
        print("FAIL: other-project identifiers present in the public repository")
        for violation in violations:
            print(" -", violation)
        print(f"\n{len(violations)} violation(s)")
        return 1

    print("PASS: no other-project identifiers found in repository text files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
