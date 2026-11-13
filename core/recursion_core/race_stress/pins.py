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

``git ls-files`` does not list children of an untracked symlink or of a nested git directory,
so those names are not trusted. ``os.walk(followlinks=False)`` and ``lstat`` refuse any
symlink under ``core/`` unless that path is a symlink in ``HEAD`` and the link text matches,
any nested ``.git`` file or directory under ``core/``, and any untracked directory directly
in ``core/`` or in a measured package directory. Each tracked file's parent chain must be
real directories, again by ``lstat`` rather than by following links.

``git`` is ``/usr/bin/git`` when that binary exists, not whatever ``PATH`` names. The bytes of
that binary are not pinned, and ``GIT_EXEC_PATH`` can still redirect its subcommands.
Environment variables that point ``git`` at a different repository are ignored. A sourceless
``foo.pyc`` beside a file pin loses to ``foo.py`` in FileFinder, so it is not treated as a
shadow while the pinned source is present. An extra ``helper.py`` next to a file pin is a
different module name; a directory pin still refuses it. An empty untracked directory directly
under ``core/`` or a measured package directory is refused: the name is importable even when
``git`` does not list the directory. Elsewhere, an empty directory is invisible to ``git`` and,
as a namespace, does not beat a tracked module or a later stdlib module.

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
import stat
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


def _parent_chain_problem(root: Path, rel: str) -> str | None:
    """The first parent of ``rel`` that is not a real directory, or None.

    Intermediate symlinks are not followed: a tracked directory replaced by a symlink
    still has the pinned bytes behind it, and ``lstat`` is what shows the swap.
    """
    parts = Path(rel).parts[:-1]
    if not parts:
        return None
    try:
        dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return f"{rel} parent chain cannot be opened"
    try:
        acc: list[str] = []
        for part in parts:
            acc.append(part)
            shown = "/".join(acc)
            try:
                st = os.stat(part, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                return f"{shown} is not a real directory"
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                return f"{shown} is not a real directory"
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dirfd)
            except OSError:
                return f"{shown} is not a real directory"
            os.close(dirfd)
            dirfd = nxt
        return None
    finally:
        try:
            os.close(dirfd)
        except OSError:
            pass


def _import_search_dirs(paths: list[str]) -> set[str]:
    """Directories searched for the pinned imports once ``core/`` is on ``sys.path``.

    ``core`` itself, every ancestor of a pinned path, a pinned directory, and the
    parent of a pinned file. Children of those directories are importable names.
    """
    dirs = {"core"}
    for path in paths:
        parts = Path(path).parts
        if not parts or parts[0] != "core":
            continue
        stop = len(parts) - 1 if _has_import_suffix(parts[-1]) else len(parts)
        acc: list[str] = []
        for part in parts[:stop]:
            acc.append(part)
            dirs.add("/".join(acc))
    return dirs


def _head_index(root: Path) -> tuple[dict[str, tuple[str, str]], set[str]] | None:
    """``path -> (mode, oid)`` and tracked directories for ``HEAD:core``. None if unusable."""
    out = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", "core")
    if out is None or out.returncode != 0:
        return None
    files: dict[str, tuple[str, str]] = {}
    dirs: set[str] = set()
    for rec in out.stdout.split("\0"):
        if not rec:
            continue
        try:
            meta, name = rec.split("\t", 1)
            mode, kind, oid = meta.split(" ")
        except ValueError:
            return None
        if Path(name).is_absolute() or ".." in Path(name).parts:
            return None
        if kind == "commit":
            dirs.add(name)
            continue
        if kind != "blob":
            return None
        files[name] = (mode, oid)
        parent = Path(name).parent
        while parent != Path("."):
            dirs.add(parent.as_posix())
            parent = parent.parent
    return files, dirs


def _symlink_blob_matches(root: Path, path: Path, oid: str) -> bool:
    try:
        out = subprocess.run(
            [GIT, "-C", str(root), "cat-file", "blob", oid],
            check=False, capture_output=True, timeout=15, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    try:
        link = os.fsencode(os.readlink(path))
    except OSError:
        return False
    return link == out.stdout


def _layout_problems(root: Path, pin_paths: list[str]) -> list[str]:
    """Symlinks, nested git entries, and importable directories under ``core/``.

    The walk does not follow symlinks and does not trust ``git ls-files`` output.
    A symlink is kept only when ``HEAD`` records that path as a symlink and the
    worktree link text is that blob.
    """
    root_abs = Path(os.path.normpath(os.path.abspath(root)))
    core = root_abs / "core"
    try:
        st = core.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [f"core cannot be statted ({exc.strerror})"]
    if stat.S_ISLNK(st.st_mode):
        return ["symlink core"]
    if not stat.S_ISDIR(st.st_mode):
        return ["core is not a directory"]
    indexed = _head_index(root_abs)
    if indexed is None:
        return ["could not list tracked files under core/"]
    tracked, tracked_dirs = indexed
    search = _import_search_dirs(pin_paths)
    problems: list[str] = []
    for dirpath, dirnames, filenames in os.walk(core, followlinks=False):
        rel_dir = Path(os.path.normpath(dirpath)).relative_to(root_abs).as_posix()
        parent_relevant = rel_dir in search
        prune: set[str] = set()
        for name in list(dirnames) + list(filenames):
            full = Path(dirpath) / name
            rel = f"{rel_dir}/{name}"
            try:
                ent = full.lstat()
            except OSError:
                problems.append(f"{rel} cannot be statted")
                prune.add(name)
                continue
            if name == ".git":
                problems.append(f"nested git {rel}")
                prune.add(name)
                continue
            if stat.S_ISLNK(ent.st_mode):
                mode_oid = tracked.get(rel)
                if (mode_oid is not None and mode_oid[0] == "120000"
                        and _symlink_blob_matches(root_abs, full, mode_oid[1])):
                    prune.add(name)
                    continue
                problems.append(f"symlink {rel}")
                prune.add(name)
                continue
            if (parent_relevant and stat.S_ISDIR(ent.st_mode)
                    and rel not in tracked_dirs and rel not in tracked):
                problems.append(f"untracked importable {rel}")
            elif (rel_dir == "core" and _has_import_suffix(name) and rel not in tracked):
                problems.append(f"untracked importable {rel}")
        dirnames[:] = [d for d in dirnames if d not in prune]
    return problems


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


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
        chain = _parent_chain_problem(root, name)
        if chain:
            problems.append(chain)
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
    problems.extend(_layout_problems(root, list(pin["objects"])))
    problems = _dedupe(problems)
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
