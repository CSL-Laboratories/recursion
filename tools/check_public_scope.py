#!/usr/bin/env python3
"""Public-scope gate: Recursion's public repository must not embed other projects.

Scans text files for identifiers that belong to other repositories or tracks with their own
lane. Deterministic and provider-free. Run from the repository root:

    python -X utf8 -B tools/check_public_scope.py

A pass certifies that no known foreign identifier remains in the readable UTF-8 files the scan visits (excluding SKIP_DIRS, SKIP_SUFFIX and this file).
A path that is not a regular file, or a symlink that leaves the repository, refuses the pass and is not opened. Bytes that are not valid UTF-8, and a file that cannot be read, also refuse the pass; their contents are not echoed.
It does not certify git history, which is a separate publication decision.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
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
SKIP_SUFFIX = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".zip",
}


def iter_files():
    for dirpath, dirs, files in os.walk(ROOT, topdown=True):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(dirpath) / name
            if path.resolve() == SELF:
                continue
            if path.suffix.lower() in SKIP_SUFFIX:
                continue
            yield path


def _visible(text: str) -> str:
    """One diagnostic line. Controls and lone surrogates cannot forge another line."""
    parts: list[str] = []
    for char in text:
        if unicodedata.category(char) in ("Cc", "Cs"):
            parts.append(f"\\u{ord(char):04x}")
        else:
            parts.append(char)
    return "".join(parts)


def _inside_root(path: Path) -> bool:
    root = ROOT.resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _refusal(path: Path) -> str | None:
    """Why this path must not be opened, or None when it is a regular in-tree file."""
    try:
        # Stat the link itself. A FIFO or a link out of the tree must not be opened:
        # opening a FIFO blocks, and opening an outside link echoes text this gate
        # was not asked to read.
        if path.is_symlink() and not _inside_root(path):
            return "symlink leaves the repository"
        if not path.is_file():
            return "not a regular file"
    except OSError:
        return "unreadable"
    return None


def main() -> int:
    violations: list[str] = []
    for path in iter_files():
        rel = _visible(str(path.relative_to(ROOT)))
        refusal = _refusal(path)
        if refusal is not None:
            violations.append(f"{rel}: {refusal}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            violations.append(f"{rel}: not valid UTF-8")
            continue
        except OSError:
            violations.append(f"{rel}: unreadable")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = PATTERN.search(line)
            if match:
                snippet = _visible(line.strip()[:100])
                violations.append(f"{rel}:{number}: '{_visible(match.group(0))}': {snippet}")

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
