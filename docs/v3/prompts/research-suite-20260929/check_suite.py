#!/usr/bin/env python3
"""Deterministic structural gate for the Deep Research prompt suite.

Checks that every DS prompt exists, carries the required front matter and headings,
uses an inert status, has a unique id, is referenced by README.md, and that relative
Markdown links resolve. Scans for obvious secret material. No network, no model.

Run from the repository root:

    python -X utf8 -B docs/v3/prompts/research-suite-20260929/check_suite.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SUITE = Path(__file__).resolve().parent

REQUIRED_HEADINGS = [
    "## Purpose",
    "## Authority and boundaries",
    "## Inputs to read",
    "## Research questions",
    "## Deliverable",
    "## Non-goals",
    "## Completion gate",
]

ALLOWED_STATUS = {
    "proposed-inert",
    "ready-for-explicit-invocation",
    "ready-for-owner-invocation",
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(password|passwd|api[_-]?key|client[_-]?secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9/_\-]{12,}"
    ),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
]

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FILE_RE = re.compile(r"^DS-(\d{2})-([a-z0-9-]+)\.md$")


def parse_front_matter(text: str):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    fm = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fm[key.strip()] = value.strip()
    return fm


def check_links(path: Path, text: str, errors: list[str]) -> None:
    for target in LINK_RE.findall(text):
        target = target.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        candidate = (path.parent / target).resolve()
        if not candidate.exists():
            errors.append(f"{path.name}: relative link does not resolve: {target}")


def main() -> int:
    errors: list[str] = []

    readme = SUITE / "README.md"
    if not readme.exists():
        print(f"FAIL: README.md missing in {SUITE}")
        return 1
    readme_text = readme.read_text(encoding="utf-8")

    ds_files = sorted(SUITE.glob("DS-*.md"))
    if not ds_files:
        print(f"FAIL: no DS-*.md files found in {SUITE}")
        return 1

    seen_ids: dict[str, str] = {}
    for path in ds_files:
        rel = path.name
        match = FILE_RE.match(rel)
        if not match:
            errors.append(f"{rel}: filename must match DS-NN-slug.md")
            continue

        ds_id = "DS-" + match.group(1)
        if ds_id in seen_ids:
            errors.append(f"{rel}: duplicate id {ds_id} (also {seen_ids[ds_id]})")
        seen_ids[ds_id] = rel

        if rel not in readme_text:
            errors.append(f"{rel}: not referenced by README.md")

        text = path.read_text(encoding="utf-8")

        fm = parse_front_matter(text)
        if fm is None:
            errors.append(f"{rel}: missing YAML front matter")
        else:
            for key in ("title", "status", "last-updated", "parent-goal"):
                if not fm.get(key):
                    errors.append(f"{rel}: front matter missing '{key}'")
            if fm.get("status") not in ALLOWED_STATUS:
                errors.append(f"{rel}: status '{fm.get('status')}' is not an allowed inert status")

        for heading in REQUIRED_HEADINGS:
            if heading not in text:
                errors.append(f"{rel}: missing heading '{heading}'")

        for pattern in SECRET_PATTERNS:
            for found in pattern.finditer(text):
                errors.append(f"{rel}: possible secret material: {found.group(0)[:40]!r}")

        check_links(path, text, errors)

    check_links(readme, readme_text, errors)

    if errors:
        print("FAIL: prompt suite structural gate")
        for error in errors:
            print(" -", error)
        return 1

    print(
        "PASS: {n} DS prompts; front matter, headings, unique ids, README refs, "
        "relative links and secret scan all clean".format(n=len(ds_files))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
