"""Pinned heads for the trees this suite measures (CSL-801), and the check that enforces them.

The measured code lives on other draft PRs, so each test points at a checkout through
``RACE_STRESS_TREE_324`` / ``_326`` / ``_328``. A checkout is used only when every measured
path's committed object is the one pinned below and the worktree bytes of every tracked file
under that path hash to that object's blobs. Those bytes are what the driver imports. A dirty
tracked file is refused even when ``HEAD`` is the pinned commit. ``skip-worktree`` and
``assume-unchanged`` do not hide an edit, and a staged blob is accepted when the worktree
bytes were restored to the pinned bytes.

Untracked and ignored files are refused when Python could import them instead of that source.
FileFinder tries a package directory (``foo/__init__.*``) before ``foo.py``, and an extension
suffix before source. A timestamp-matched or unchecked-hash ``.pyc`` is executed even under
``-B``, while ``module.__file__`` still names the ``.py``. Under a directory pin every
untracked path is refused except a file that is provably not importable: its name has no
source, bytecode, or extension suffix, and every parent directory is already tracked (a new
directory is a namespace package). A file pin also refuses a sibling ``__pycache__``, an
extension module with the pinned stem, and a package directory with that stem. An untracked
top-level importable name under ``core/`` is refused too: the drivers insert that directory
on ``sys.path``, so ``core/dataclasses.py`` would shadow the standard library.

``git`` is ``/usr/bin/git`` when that binary exists, not whatever ``PATH`` names. The bytes of
that binary are not pinned, and ``GIT_EXEC_PATH`` can still redirect its subcommands.
Environment variables that point ``git`` at a different repository are ignored. A sourceless
``foo.pyc`` beside a file pin loses to ``foo.py`` in FileFinder, so it is not treated as a
shadow while the pinned source is present. An extra ``helper.py`` next to a file pin is a
different module name; a directory pin still refuses it. An empty untracked directory is
invisible to ``git`` and, as a namespace, does not beat a tracked module or a later stdlib
module.

Anything else skips with a message naming both heads, instead of measuring code nobody pinned.
Set ``RACE_STRESS_UNPINNED=1`` to run against an unpinned tree anyway (the run then reports
which tree it measured).

Re-pin by updating ``head`` and ``objects`` after re-running the suite on the new head:
``git -C <tree> rev-parse HEAD HEAD:<path>`` for each path.
"""

from __future__ import annotations

import importlib.machinery
import os
import shutil
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

_IMPORT_SUFFIXES = tuple(
    importlib.machinery.EXTENSION_SUFFIXES
    + importlib.machinery.SOURCE_SUFFIXES
    + importlib.machinery.BYTECODE_SUFFIXES)


def _resolve_git() -> str:
    """Absolute git binary. ``/usr/bin/git`` wins over ``PATH``.

    Residual: that binary's bytes are not pinned, and ``GIT_EXEC_PATH`` (left in the
    environment; it is not a repo selector) can still redirect git's subcommands.
    """
    trusted = "/usr/bin/git"
    if os.path.isfile(trusted) and os.access(trusted, os.X_OK):
        return trusted
    found = shutil.which("git")
    if found and os.path.isabs(found) and os.access(found, os.X_OK):
        return found
    return trusted


GIT = _resolve_git()


def git_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in _GIT_REPO_ENV and not k.startswith("GIT_CONFIG_")}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run([GIT, "-C", str(root), *args], check=False, capture_output=True,
                              text=True, timeout=15, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _git_text(root: Path, *args: str) -> str | None:
    out = _git(root, *args)
    if out is None or out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _git_z(root: Path, *args: str) -> list[str] | None:
    out = _git(root, *args)
    if out is None or out.returncode != 0:
        return None
    return [rec for rec in out.stdout.split("\0") if rec]


def _has_import_suffix(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _IMPORT_SUFFIXES)


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


def _untracked(root: Path, path: str) -> list[str] | None:
    """Untracked files under ``path``, including ignored ones.

    ``--exclude-standard`` is omitted on purpose: exclude rules are not active, so a
    gitignored ``__pycache__`` or ``*.ignored.py`` is still listed. ``git status`` is not.
    """
    return _git_z(root, "ls-files", "-z", "--others", "--", path)


def _allowed_untracked(root: Path, pin: str, rel: str) -> bool:
    """True when ``rel`` cannot be selected by FileFinder and creates no new package directory."""
    try:
        inside = Path(rel).relative_to(pin)
    except ValueError:
        return False
    if "__pycache__" in Path(rel).parts or _has_import_suffix(Path(rel).name):
        return False
    parent = Path(pin)
    for part in inside.parts[:-1]:
        parent = parent / part
        if _git_text(root, "cat-file", "-t", f"HEAD:{parent.as_posix()}") != "tree":
            return False
    return True


def _directory_pin_shadows(root: Path, path: str, blobs: list[tuple[str, str]]) -> list[str]:
    found: list[str] = []
    for name, _blob in blobs:
        base = Path(name).name
        if "__pycache__" in Path(name).parts:
            found.append(f"bytecode cache {name}")
        elif _has_import_suffix(base) and not base.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES)):
            found.append(f"non-source import {name}")
    untracked = _untracked(root, path)
    if untracked is None:
        return found + ["could not list untracked files"]
    for rel in untracked:
        if not _allowed_untracked(root, path, rel):
            found.append(f"untracked importable {rel}")
    return found


def _module_stem(filename: str) -> str:
    for suffix in importlib.machinery.SOURCE_SUFFIXES:
        if filename.endswith(suffix):
            return filename[:-len(suffix)]
    return Path(filename).stem


def _file_pin_shadows(root: Path, path: str) -> list[str]:
    parent = Path(path).parent.as_posix()
    stem = _module_stem(Path(path).name)
    found: list[str] = []
    cache = f"{parent}/__pycache__"
    cached = _untracked(root, cache)
    if cached is None:
        found.append(f"could not list {cache}")
    else:
        found.extend(f"bytecode cache {name}" for name in cached)
    tracked = _git_z(root, "ls-files", "-z", "--", cache)
    if tracked is None:
        found.append(f"could not list {cache}")
    else:
        found.extend(f"bytecode cache {name}" for name in tracked)
    others = _untracked(root, parent)
    if others is None:
        return found + [f"could not list untracked files beside {path}"]
    prefix = f"{parent}/{stem}/"
    for rel in others:
        name = Path(rel).name
        if Path(rel).parent.as_posix() == parent:
            if any(name == stem + suffix for suffix in importlib.machinery.EXTENSION_SUFFIXES):
                found.append(f"extension shadow {rel}")
        elif rel.startswith(prefix) and name.startswith("__init__") and _has_import_suffix(name):
            found.append(f"package shadow {rel}")
    for suffix in _IMPORT_SUFFIXES:
        init = f"{prefix}__init__{suffix}"
        if _git_text(root, "cat-file", "-t", f"HEAD:{init}") == "blob":
            found.append(f"package shadow {init}")
    return found


def _core_path_shadows(root: Path) -> list[str]:
    """Untracked importable names directly under ``core/``, the directory drivers put on sys.path."""
    if not (root / "core").is_dir():
        return []
    others = _untracked(root, "core")
    if others is None:
        return ["could not list untracked files under core/"]
    found: list[str] = []
    tracked: dict[str, bool] = {}
    for rel in others:
        under = Path(rel).relative_to("core")
        if not under.parts:
            continue
        top = under.parts[0]
        if top not in tracked:
            tracked[top] = _git_text(root, "cat-file", "-t", f"HEAD:core/{top}") in ("blob", "tree")
        if tracked[top]:
            continue
        if len(under.parts) == 1 and _has_import_suffix(top):
            found.append(f"untracked top-level import {rel}")
        elif (len(under.parts) >= 2 and under.parts[1].startswith("__init__")
              and _has_import_suffix(under.parts[1])):
            found.append(f"untracked top-level package {rel}")
    return found


def _path_problems(root: Path, path: str, oid: str) -> list[str]:
    """Reasons ``path`` is not the pinned object. Empty when the worktree bytes and imports match."""
    if _git_text(root, "rev-parse", f"HEAD:{path}") != oid:
        return ["committed object is not the pin"]
    blobs = _tracked_blobs(root, path)
    if not blobs:
        return ["tracked file list is unusable"]
    problems: list[str] = []
    for name, _blob in blobs:
        file = root / name
        if not file.is_file() or file.is_symlink():
            problems.append(f"{name} missing or is a symlink")
    if not problems:
        out = _git(root, "hash-object", "--no-filters", "--", *[name for name, _blob in blobs])
        if out is None or out.returncode != 0:
            problems.append("could not hash worktree bytes")
        else:
            hashed = out.stdout.splitlines()
            if len(hashed) != len(blobs) or any(
                    got != blob for (_name, blob), got in zip(blobs, hashed)):
                problems.append("worktree bytes differ")
    if len(blobs) == 1 and blobs[0][0] == path:
        problems.extend(_file_pin_shadows(root, path))
    else:
        problems.extend(_directory_pin_shadows(root, path, blobs))
    return problems


def pin_status(env: str, root: Path) -> tuple[bool, str]:
    """``(ok, message)`` for the checkout ``root`` named by ``env``."""
    pin = PINS[env]
    head = _git_text(root, "rev-parse", "HEAD")
    if head is None:
        return False, f"{env}={root} is not a git checkout; cannot check it against the pin {pin['head'][:8]}"
    problems = [f"{path}: {reason}" for path, oid in pin["objects"].items()
                for reason in _path_problems(root, path, oid)]
    problems.extend(_core_path_shadows(root))
    if not problems:
        if head == pin["head"]:
            return True, f"{env} at pinned head {head[:8]}"
        return True, f"{env} at {head[:8]}; measured paths identical to pinned head {pin['head'][:8]}"
    where = (f"{env} is at pinned head {head[:8]}, but " if head == pin["head"] else
             f"{env} is at {head[:8]}, not the pinned {pin['pr']} head {pin['head'][:8]}, and ")
    return False, (where + "; ".join(problems) + "; re-run and re-pin in "
                   f"race_stress/pins.py, or set RACE_STRESS_UNPINNED=1 to measure it anyway")


def require_pinned(env: str, root: Path) -> None:
    """Skip the calling test unless ``root`` is the pinned tree (or RACE_STRESS_UNPINNED=1)."""
    ok, message = pin_status(env, root)
    if not ok and os.environ.get("RACE_STRESS_UNPINNED") != "1":
        raise unittest.SkipTest(message)
