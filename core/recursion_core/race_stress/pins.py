"""Pinned heads for the trees this suite measures (CSL-801), and the check that enforces them.

The measured code lives on other draft PRs, so each test points at a checkout through
``RACE_STRESS_TREE_324`` / ``_326`` / ``_328``. A checkout is used only when every measured
path's committed object is the one pinned below and the worktree bytes of every tracked file
under that path hash to that object's blobs. Those bytes are what the driver imports. A dirty
tracked file is refused even when ``HEAD`` is the pinned commit. ``skip-worktree`` and
``assume-unchanged`` do not hide an edit, and a staged blob is accepted when the worktree
bytes were restored to the pinned bytes. Untracked files are not compared: the measured
import executes the tracked modules, so a scratch file under the pin is not imported unless
a tracked module names it. Environment variables that point ``git`` at a different repository
are ignored.
Anything else skips with a message naming both heads, instead of measuring code nobody pinned.
Set ``RACE_STRESS_UNPINNED=1`` to run against an unpinned tree anyway (the run then reports
which tree it measured).

Re-pin by updating ``head`` and ``objects`` after re-running the suite on the new head:
``git -C <tree> rev-parse HEAD HEAD:<path>`` for each path.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

PINS: dict[str, dict] = {
    "RACE_STRESS_TREE_324": {
        "pr": "recursion #324 (CSL-738)",
        "head": "7c729db9efd01c841cc13ebbd62e051b38b13397",
        "objects": {"core/recursion_core/daemon": "726edd12e227ccf625870091fcfc48d2ee06caa1"},
    },
    "RACE_STRESS_TREE_326": {
        "pr": "recursion #326 (CSL-797)",
        "head": "3147f97929091c80662f2b3752b283d66944a83d",
        "objects": {
            "core/recursion_core/routes/breaker.py": "e27e941c4e2d88196801923fedc0e4411ca5127d",
            "core/recursion_core/routes/contract.py": "3f7bb8e03eaec4ab000169d5a04f55383ffe8c1d",
        },
    },
    "RACE_STRESS_TREE_328": {
        "pr": "recursion #328 (CSL-720)",
        "head": "54a2d77128a0d3f1412cbf08f0f8fbec5a4520ff",
        "objects": {
            "core/recursion_core/plugin_security/breaker.py": "39fadf060d01bd5d94200d4a7d493f8f1da57ae0",
            "core/recursion_core/plugin_security/errors.py": "bb644bc884fa133a0487688ac9cd617b4bdcc6b0",
        },
    },
}


# These select a repository other than the checkout ``git -C`` was given. GIT_CONFIG_* is the
# same class of override (config can set core.worktree).
_GIT_REPO_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_INDEX_FILE",
    "GIT_NAMESPACE", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES",
)


def git_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in _GIT_REPO_ENV and not k.startswith("GIT_CONFIG_")}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True,
                              text=True, timeout=15, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _git_text(root: Path, *args: str) -> str | None:
    out = _git(root, *args)
    if out is None or out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _tracked_blobs(root: Path, path: str) -> list[tuple[str, str]] | None:
    """``(path, blob)`` for every file at ``HEAD`` under ``path``. None if that list is unusable."""
    out = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", path)
    if out is None or out.returncode != 0:
        return None
    blobs: list[tuple[str, str]] = []
    for rec in out.stdout.split("\0"):
        if not rec:
            continue
        try:
            meta, name = rec.split("\t", 1)
            mode, kind, oid = meta.split(" ")
        except ValueError:
            return None
        if (kind != "blob" or mode not in ("100644", "100755")
                or Path(name).is_absolute() or ".." in Path(name).parts):
            return None
        blobs.append((name, oid))
    return blobs


def _worktree_matches(root: Path, path: str, oid: str) -> bool:
    """True when ``HEAD:path`` is ``oid`` and every tracked file's worktree bytes match.

    Untracked files are not compared. Importing the pinned package executes the tracked
    modules; an untracked ``scratch.py`` or ``tests/local_note.py`` is not on that path
    unless a tracked module names it, and that tracked file is hashed here. ``git status``
    refuses any untracked file and misses edits hidden by ``skip-worktree`` or
    ``assume-unchanged``. ``git hash-object --no-filters`` reads the bytes Python imports,
    which also matches a worktree restored to ``HEAD`` while the index still holds another
    blob.
    """
    if _git_text(root, "rev-parse", f"HEAD:{path}") != oid:
        return False
    blobs = _tracked_blobs(root, path)
    if not blobs:
        return False
    for name, _blob in blobs:
        file = root / name
        if not file.is_file() or file.is_symlink():
            return False
    out = _git(root, "hash-object", "--no-filters", "--", *[name for name, _blob in blobs])
    if out is None or out.returncode != 0:
        return False
    hashed = out.stdout.splitlines()
    if len(hashed) != len(blobs):
        return False
    return all(got == blob for (_name, blob), got in zip(blobs, hashed))


def pin_status(env: str, root: Path) -> tuple[bool, str]:
    """``(ok, message)`` for the checkout ``root`` named by ``env``."""
    pin = PINS[env]
    head = _git_text(root, "rev-parse", "HEAD")
    if head is None:
        return False, f"{env}={root} is not a git checkout; cannot check it against the pin {pin['head'][:8]}"
    moved = [p for p, oid in pin["objects"].items() if not _worktree_matches(root, p, oid)]
    if not moved:
        if head == pin["head"]:
            return True, f"{env} at pinned head {head[:8]}"
        return True, f"{env} at {head[:8]}; measured paths identical to pinned head {pin['head'][:8]}"
    where = (f"{env} is at pinned head {head[:8]}, but " if head == pin["head"] else
             f"{env} is at {head[:8]}, not the pinned {pin['pr']} head {pin['head'][:8]}, and ")
    return False, (where + f"{', '.join(moved)} differ from the pinned bytes; re-run and re-pin in "
                   f"race_stress/pins.py, or set RACE_STRESS_UNPINNED=1 to measure it anyway")


def require_pinned(env: str, root: Path) -> None:
    """Skip the calling test unless ``root`` is the pinned tree (or RACE_STRESS_UNPINNED=1)."""
    ok, message = pin_status(env, root)
    if not ok and os.environ.get("RACE_STRESS_UNPINNED") != "1":
        raise unittest.SkipTest(message)
