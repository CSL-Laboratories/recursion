#!/usr/bin/env python3
"""Deterministic structural gate for the 2026-10-02g Dot research packet.

Checks that every ``R-*.md`` handoff exists, carries the required front matter and the eight
required headings, contains the research directive, is referenced by ``README.md``, and that
relative Markdown links resolve. Reuses the forbidden-identifier pattern from
``tools.check_public_scope`` and scans for obvious secret material. No network, no model.

    python -X utf8 -B docs/v3/prompts/dot-research-20261002g/check_research_stack.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SUITE = Path(__file__).resolve().parent

ROOT = SUITE
for _ in range(6):
    if (ROOT / "tools" / "check_public_scope.py").is_file():
        break
    ROOT = ROOT.parent
else:
    print("FAIL: could not locate tools/check_public_scope.py above the suite directory")
    raise SystemExit(1)

sys.path.insert(0, str(ROOT))
try:
    from tools.check_public_scope import PATTERN as FORBIDDEN_PATTERN
except Exception as exc:  # pragma: no cover - defensive
    print(f"FAIL: could not import tools.check_public_scope.PATTERN: {exc}")
    raise SystemExit(1)

EXPECTED = [
    "R-PLAN-planning-and-executives-facet.md",
    "R-OPS-operations-lifecycle-facet.md",
]

REQUIRED_HEADINGS = [
    "## Baked context",
    "## Purpose",
    "## Authority and boundaries",
    "## Inputs to read",
    "## Research questions",
    "## Deliverable",
    "## Non-goals",
    "## Completion gate",
]

REQUIRED_FRONT_MATTER = ("title", "status", "last-updated", "parent-goal")
README_FRONT_MATTER = ("title", "status", "last-updated", "parent-goal", "invocation")
ALLOWED_STATUS = {"ready-for-explicit-invocation"}

DIRECTIVE_MARK = "**Research directive.** You are the lead researcher."
INERT_MARKERS = (
    "proposed-inert",
    "inert until",
    "This file is inert",
    "reading or creating it launches nothing",
)

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
        if not (path.parent / target).resolve().exists():
            errors.append(f"{path.name}: relative link does not resolve: {target}")


def check_forbidden(path: Path, text: str, errors: list[str]) -> None:
    for number, line in enumerate(text.splitlines(), 1):
        match = FORBIDDEN_PATTERN.search(line)
        if match:
            errors.append(f"{path.name}:{number}: other-project identifier '{match.group(0)}'")


def check_secrets(path: Path, text: str, errors: list[str]) -> None:
    for pattern in SECRET_PATTERNS:
        for found in pattern.finditer(text):
            errors.append(f"{path.name}: possible secret material: {found.group(0)[:40]!r}")


def main() -> int:
    errors: list[str] = []

    readme = SUITE / "README.md"
    if not readme.exists():
        print(f"FAIL: README.md missing in {SUITE}")
        return 1
    readme_text = readme.read_text(encoding="utf-8")
    fm = parse_front_matter(readme_text)
    if fm is None:
        errors.append("README.md: missing YAML front matter")
    else:
        for key in README_FRONT_MATTER:
            if not fm.get(key):
                errors.append(f"README.md: front matter missing '{key}'")
        if fm.get("status") not in ALLOWED_STATUS:
            errors.append(f"README.md: status '{fm.get('status')}' is not 'ready-for-explicit-invocation'")

    r_files = sorted(SUITE.glob("R-*.md"))
    if not r_files:
        print(f"FAIL: no R-*.md files found in {SUITE}")
        return 1
    found_names = {path.name for path in r_files}
    for name in EXPECTED:
        if name not in found_names:
            errors.append(f"{name}: expected handoff is missing")
    for name in sorted(found_names - set(EXPECTED)):
        errors.append(f"{name}: unexpected R file (the packet is fixed at two handoffs)")

    seen_titles: dict[str, str] = {}
    for path in r_files:
        rel = path.name
        text = path.read_text(encoding="utf-8")
        if rel not in readme_text:
            errors.append(f"{rel}: not referenced by README.md")
        front = parse_front_matter(text)
        if front is None:
            errors.append(f"{rel}: missing YAML front matter")
        else:
            for key in REQUIRED_FRONT_MATTER:
                if not front.get(key):
                    errors.append(f"{rel}: front matter missing '{key}'")
            if front.get("status") not in ALLOWED_STATUS:
                errors.append(f"{rel}: status '{front.get('status')}' is not 'ready-for-explicit-invocation'")
            title = front.get("title")
            if title:
                if title in seen_titles:
                    errors.append(f"{rel}: duplicate title (also {seen_titles[title]})")
                seen_titles[title] = rel
        for heading in REQUIRED_HEADINGS:
            if heading not in text:
                errors.append(f"{rel}: missing heading '{heading}'")
        if DIRECTIVE_MARK not in text:
            errors.append(f"{rel}: missing research directive line '{DIRECTIVE_MARK}'")
        for marker in INERT_MARKERS:
            if marker in text:
                errors.append(f"{rel}: inert framing is not allowed: '{marker}'")
        check_forbidden(path, text, errors)
        check_secrets(path, text, errors)
        check_links(path, text, errors)

    check_forbidden(readme, readme_text, errors)
    check_secrets(readme, readme_text, errors)
    check_links(readme, readme_text, errors)

    if errors:
        print("FAIL: Dot research packet structural gate")
        for error in errors:
            print(" -", error)
        return 1
    print(
        "PASS: {n} R handoffs; README and handoff front matter, headings, research directive, "
        "unique titles, README refs, relative links, forbidden-identifier reuse and secret scan "
        "all clean".format(n=len(r_files))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
