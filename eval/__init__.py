"""Recursion's evaluation harness (lane EVAL): runner, run record, pass^k calculator, harvest.

Nothing here is product code. The package measures an engine run directly against the same engine
run under Recursion, writes one ``recursion-eval-run/1`` record per suite run and recomputes every
score from the trials in that record. PROTOCOL.md says what is measured and how; the analysis
script and the calculator it uses are hashed in FROZEN.sha256.

Standard library only: the free-threaded interpreter the core runs on has no third-party package
but ``cryptography``, and this package needs none.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

# The files whose bytes define the runner. Their combined digest goes into every record and is what
# a frozen suite manifest pins. Tests and data are not part of it.
RUNNER_FILES = (
    "__init__.py",
    "arms.py",
    "heldout.py",
    "passk.py",
    "record.py",
    "runner.py",
    "scratch.py",
    "standin_engine.py",
    "suite.py",
    "tasks.py",
    "txtar.py",
    "gitutil.py",
)


def files_digest(root: Path, names) -> str:
    """One SHA-256 over several files: sha256 of the lines ``<sha256>  <name>`` in name order."""
    lines = []
    for name in sorted(names):
        data = (root / name).read_bytes()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def runner_sha256() -> str:
    return files_digest(PACKAGE_ROOT, RUNNER_FILES)
