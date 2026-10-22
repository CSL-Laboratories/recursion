#!/usr/bin/env python3
"""Readiness gate for the next phase (FU-03/FU-04 experiments, then FU-01/02/05).

Checks that the local machine has the toolchains the next phase needs, that the follow-up
specifications are present, and that the working tree is a clean git checkout. Deterministic and
provider-free. Exits non-zero while anything required is missing.

Run from the repository root:

    python -X utf8 -B tools/check_next_phase.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_SPECS = [
    "docs/v3/research-lane/NEXT-PHASE.md",
    "docs/v3/research-lane/SYNTHESIS.md",
    "docs/v3/research-lane/GAPS.md",
    "docs/v3/research-lane/followups-20261002/raw/ds-FU-03-formal-model-vectors.md",
    "docs/v3/research-lane/followups-20261002/raw/ds-FU-04-os-conformance.md",
    "docs/v3/research-lane/followups-20261002/machine/platform-manifest.json",
]

# tool -> whether it is mandatory for the immediate next phase (FU-03/FU-04)
TOOLS = [
    ("python", True),
    ("git", True),
    ("node", False),
    ("java", True),      # TLA+/TLC for FU-03
    ("rustc", False),    # FU-01 (later)
    ("cargo", False),
    ("go", False),
]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def find_tool(name: str) -> str | None:
    """Locate a tool on PATH or in the usual Windows install locations."""
    found = shutil.which(name)
    if found:
        return found
    exe = f"{name}.exe"
    candidates = [
        Path("C:/Program Files/Go/bin") / exe,
        Path.home() / ".cargo" / "bin" / exe,
    ]
    for pattern in ("Eclipse Adoptium", "Eclipse Foundation"):
        base = Path("C:/Program Files") / pattern
        if base.exists():
            candidates.extend(sorted(base.glob(f"*/bin/{exe}")))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def main() -> int:
    gaps: list[str] = []
    notes: list[str] = []

    py_ok = sys.version_info >= (3, 11)
    print(f"python {sys.version.split()[0]}: {'OK' if py_ok else 'TOO OLD (need 3.11+)'}")
    if not py_ok:
        gaps.append("python >= 3.11")

    for name, mandatory in TOOLS:
        if name == "python":
            continue
        found = find_tool(name)
        state = f"OK ({found})" if found else ("MISSING" if mandatory else "missing (not needed for FU-03/04)")
        print(f"{name}: {state}")
        if not found and mandatory:
            gaps.append(name)

    for rel in REQUIRED_SPECS:
        ok = (ROOT / rel).exists()
        print(f"spec {rel}: {'OK' if ok else 'MISSING'}")
        if not ok:
            gaps.append(rel)

    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        sha = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain")
        print(f"git: branch={branch} head={sha[:12]} {'DIRTY' if dirty else 'clean'}")
        if dirty:
            notes.append("working tree is dirty; commit or stash before experiments")
    except Exception as exc:  # pragma: no cover - defensive
        gaps.append(f"git check failed: {exc}")

    print()
    if gaps:
        print("FAIL: next-phase prerequisites missing:")
        for gap in gaps:
            print(" -", gap)
        print("\nInstall with: powershell -ExecutionPolicy Bypass -File tools/setup-next-phase.ps1")
        return 1
    print("PASS: next-phase prerequisites present (Java for TLA+/TLC; Rust/Go are later, for FU-01).")
    for note in notes:
        print("note:", note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
