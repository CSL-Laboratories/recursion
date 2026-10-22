#!/usr/bin/env python3
"""Rescope gate (2026-10-02): verifies the rescoping fan-out is present and coherent.

Deterministic and provider-free. Run from the repository root:

    python -X utf8 -B tools/check_rescope.py

A pass certifies structure and link integrity only. It is not Owner acceptance, not a target
experiment, and not a release gate. The 2026-09-13 orientation gate
(`tools/check_v3_orientation.py`) is scope-frozen to its repair and does not apply to this change
set; this gate covers the rescope change set instead.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from check_public_scope import PATTERN  # noqa: E402

REQUIRED_FILES = [
    "docs/v3/INTERFACE-STRATEGY.md",
    "docs/v3/prompts/dot-research-20261002/README.md",
    "docs/v3/prompts/dot-research-20261002/R-A-plugin-portability.md",
    "docs/v3/prompts/dot-research-20261002/R-B-cross-harness-delegation.md",
    "docs/v3/prompts/dot-research-20261002/R-C-route-legal-and-licensing.md",
    "docs/v3/prompts/dot-research-20261002/R-D-quota-efficiency-measurement.md",
    "docs/v3/prompts/dot-research-20261002/check_research_stack.py",
    "docs/v3/research-lane/scope-reframe-20261002/check_intake.py",
    "docs/v3/research-lane/scope-reframe-20261002/MANIFEST.json",
]

REQUIRED_HEADINGS = {
    "docs/v3/INTERFACE-STRATEGY.md": [
        "## Division of responsibility",
        "## Three Stops",
        "## Demonstration journeys",
    ],
}

DECISION_IDS = [f"D-{n:03d}" for n in range(18, 28)]


def fail(message: str) -> None:
    print(f"RESCOPE_FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def prose(text: str) -> str:
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)
    return re.sub(r"`[^`\n]*`", "", text)


def main() -> int:
    for rel in REQUIRED_FILES:
        if not (ROOT / rel).exists():
            fail(f"missing required file: {rel}")

    decisions = read("state/DECISIONS.md")
    for dec in DECISION_IDS:
        if not re.search(rf"^## {re.escape(dec)} ", decisions, re.MULTILINE):
            fail(f"state/DECISIONS.md missing accepted decision {dec}")

    for rel, headings in REQUIRED_HEADINGS.items():
        text = read(rel)
        for heading in headings:
            if heading not in text:
                fail(f"{rel} missing required heading: {heading}")

    markdown_docs = [rel for rel in REQUIRED_FILES if rel.endswith(".md")]

    # Link integrity across the new docs.
    links = 0
    for rel in markdown_docs:
        path = ROOT / rel
        for raw in re.findall(r"\]\(([^)]+)\)", prose(path.read_text(encoding="utf-8"))):
            target = raw.split(' "', 1)[0].strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(ROOT):
                fail(f"link escapes repository: {rel}: {raw}")
            if not resolved.exists():
                fail(f"missing link target: {rel}: {raw}")
            links += 1

    # No foreign identifier in the new docs.
    for rel in markdown_docs:
        for number, line in enumerate(read(rel).splitlines(), 1):
            match = PATTERN.search(line)
            if match:
                fail(f"forbidden identifier {rel}:{number}: {match.group(0)}")

    print(f"RESCOPE_PASS files={len(REQUIRED_FILES)} decisions={len(DECISION_IDS)} links={links}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
