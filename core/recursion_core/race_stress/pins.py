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
directory is a namespace package). Every hashed module, including one imported from outside
the import-search directories, refuses a same-stem package directory (``__init__`` with any
source, bytecode, or extension suffix), a same-stem extension module, and every entry in
that directory's ``__pycache__``. Any untracked ``.pyc`` under ``core/`` is refused. An
untracked top-level importable name under ``core/`` is refused too: the drivers insert that
directory on ``sys.path``, so ``core/dataclasses.py`` would shadow the standard library.

``git ls-files`` does not list children of an untracked symlink or of a nested git directory,
so those names are not trusted. ``os.walk(followlinks=False)`` and ``lstat`` refuse every
symlink under ``core/``. A committed symlink whose link text matches ``HEAD`` is still
refused: the target bytes are not that blob, and the pinned heads have no symlinks there.
A dangling Emacs lock named ``.#*`` is the exception. Nested ``.git`` entries are refused.
An untracked directory in ``core/`` or a measured package directory is refused when it
contains a file, except a dot-directory other than ``.git`` and a ``*.egg-info`` directory,
and those two only when they contain no ``.py``, ``.pyc``, ``.so``, or ``.pth``. An empty
directory (no files at any depth) is allowed. Each tracked file's parent chain must be
real directories, again by ``lstat`` rather than by following links.

A file pin hashes more than its own blob. Importing the pinned module runs every ancestor
package ``__init__.py``, and those files import sibling modules, so each of those tracked
files is hashed against ``HEAD`` as well. A directory pin seeds that same walk with every
tracked ``.py`` under the directory, including its own ``__init__.py``, then hashes the
transitive imports and applies the shadow checks. Seeding the directory path itself skipped
the package body: its ``__init__.py`` and siblings never contributed imports.

A namespace portion (a package directory with no ``__init__`` file) loses to a sibling of
the same stem with any loader suffix, and to an ``__init__`` plus any loader suffix inside
it. Those untracked files are refused. Package-shadow names are exactly ``__init__`` plus
one suffix from ``importlib.machinery.all_suffixes()``; ``__init__extra.py`` is not a
package init.

When the import resolves to a sourceless ``.pyc``, those worktree bytes are hashed with
``git hash-object --no-filters`` and the file is not executed. A clean match stays pinned,
and a changed, missing, or untracked file is refused. The code object (marshal after the
16-byte header) is read and its ``IMPORT_NAME`` targets are walked like source imports,
including nested code objects in ``co_consts``. A dynamic import in that code refuses the
pin. An extension file in the closure is hashed the same way; imports inside it are not
walked. A tracked ``.pyc`` beside a same-stem ``.py`` is not the loaded module (source is
tried first) and stays allowed, under a file pin and under a directory pin.

Every hashed source module is scanned for calls that load code the import statements do
not name. ``importlib.import_module``, ``importlib.__import__``, ``__import__`` and
``importlib.util.spec_from_file_location`` add their target when the module name is a
string literal, and a package-relative name only when the package or level is a literal
too; any other argument refuses the pin. ``exec`` and ``compile`` of a string literal are
scanned the same way; a non-literal argument refuses the pin. ``from X import *`` reads
``X``'s ``__all__`` when that list or tuple is made of string literals and adds each
``X.name`` submodule that resolves; a non-literal ``__all__`` refuses the pin. An
assignment, augmented assignment or method call that mutates ``__path__``, ``sys.path``,
``sys.meta_path`` or ``sys.path_hooks`` refuses the pin.

``os.walk`` records listing errors and fails closed. A directory mode such as ``0111``
hides names from ``readdir``, ``os.walk``, and ``git ls-files --others`` while Python still
opens a known cache path. For each hashed source module the path from
``importlib.util.cache_from_source`` is statted directly and refused when that file is
present and untracked. An empty directory that cannot be listed is refused, because it
cannot be shown to be empty. An empty directory that lists cleanly stays allowed.

``git`` is ``/usr/bin/git`` when that binary exists, not whatever ``PATH`` names. The bytes of
that binary are not pinned, and ``GIT_EXEC_PATH`` can still redirect its subcommands.
Environment variables that point ``git`` at a different repository are ignored. An extra
``helper.py`` next to a hashed module is a different module name; a directory pin still
refuses it when it sits inside the pinned directory, and leaves it alone outside that
directory. An empty directory does not beat a tracked module or a later regular module on
``sys.path``.

The static scan is only an early refusal. It covers the modules importing the pinned
entry actually executes (the driver and the interleave loader), including function bodies
of those entry files and of sourceless bytecode, and module-level statements of the
packages those imports run. A dynamic call inside a function of some other module, or
any dynamic code in a file the run never imports, does not by itself refuse the pin.
The child that runs the stress installs ``sys.addaudithook`` before importing pinned
code and records ``import``, ``exec``, ``compile``, code-file ``open``, and
``os.add_dll_directory`` when that event exists. ``pin_status`` is true only when every
code object that child executed (module files, compiled or exec'd strings, zip members,
``.pth`` files) is in the hashed set with matching blob hashes. An exec, eval, or
compile of a string that is not the text of a hashed file, a loader that is not a file
loader, or a ``sys.path`` / ``sys.meta_path`` / ``sys.path_hooks`` / ``__path__`` change
seen on an audit event or by comparing the process before and after makes it false.

Not checked here: ``sitecustomize`` reached through ``PYTHONPATH`` when the process is not
isolated (``-B`` does not skip it); the ``PATH`` fallback when ``/usr/bin/git`` is absent;
a ``__spec__`` whose paths still name the pinned file, which ``verify_loaded`` accepts;
launching a driver as a plain script instead of the isolated runner; imports inside an
extension module; a string-literal ``spec_from_file_location`` path that is not the hashed
file. The audit hook is in-process only: a subprocess the pinned code starts is not
hooked, and ``ctypes`` (or another C call that does not go through the audited import
and eval paths) can still load code or rewrite lists without an event. A path edit that
is undone before the next recorded import and before ``pin_status`` reads the snapshot
is not seen. ``compile`` of a string whose immediate caller is the standard library
(``dataclasses``, ``collections.namedtuple``) is not attributed to the tree.

Anything else skips with a message naming both heads, instead of measuring code nobody pinned.
Set ``RACE_STRESS_UNPINNED=1`` to run against an unpinned tree anyway (the run then reports
which tree it measured).

Re-pin by updating ``head`` and ``objects`` after re-running the suite on the new head:
``git -C <tree> rev-parse HEAD HEAD:<path>`` for each path.
"""

from __future__ import annotations

import ast
import dis
import importlib.machinery
import importlib.util
import marshal
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import types
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

# FileFinder tries extension, then source, then sourceless bytecode.
_LOADER_SUFFIXES = tuple(
    importlib.machinery.EXTENSION_SUFFIXES
    + importlib.machinery.SOURCE_SUFFIXES
    + importlib.machinery.BYTECODE_SUFFIXES)
_IMPORT_SUFFIXES = _LOADER_SUFFIXES
_INIT_NAMES = frozenset("__init__" + suffix for suffix in importlib.machinery.all_suffixes())


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


def _is_init_filename(name: str) -> bool:
    """True for ``__init__`` plus one loader suffix, and not ``__init__extra.py``."""
    return name in _INIT_NAMES


def _strip_loader_suffix(name: str) -> str | None:
    for suffix in sorted(_LOADER_SUFFIXES, key=len, reverse=True):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return None


def _is_bytecode(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in importlib.machinery.BYTECODE_SUFFIXES)


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
    """Same shadow rules as a file pin for a tracked ``.pyc`` beside its ``.py``.

    FileFinder tries source before sourceless bytecode, so ``registry.pyc`` next to
    ``registry.py`` is not the loaded module. A sourceless ``.pyc`` (no same-stem
    source) is still a non-source import, and a ``__pycache__`` entry still is.
    """
    source_stems: set[tuple[str, str]] = set()
    for name, _blob in blobs:
        base = Path(name).name
        if base.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES)):
            source_stems.add((Path(name).parent.as_posix(), _module_stem(base)))
    found: list[str] = []
    for name, _blob in blobs:
        base = Path(name).name
        parent = Path(name).parent.as_posix()
        if "__pycache__" in Path(name).parts:
            found.append(f"bytecode cache {name}")
        elif _is_bytecode(base) and (parent, _module_stem(base)) in source_stems:
            continue
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
    stripped = _strip_loader_suffix(filename)
    if stripped is not None:
        return stripped
    return Path(filename).stem


def _pycache_home(rel: str) -> str | None:
    """Directory that owns the ``__pycache__`` component of ``rel``, if it has one."""
    parts = Path(rel).parts
    if "__pycache__" not in parts:
        return None
    index = parts.index("__pycache__")
    if index == 0:
        return None
    return "/".join(parts[:index])


def _file_pin_shadows(root: Path, paths: list[str]) -> list[str]:
    """Shadows of every hashed module, including imports outside the search set.

    FileFinder loads ``stem/__init__`` plus one suffix from ``all_suffixes()`` before
    ``stem.py``, and an extension suffix before source. ``__init__extra.py`` is not
    that name. Every entry in a hashed module's ``__pycache__`` can run under
    ``-B`` while ``module.__file__`` still names the ``.py``. A sibling ``helper.py``
    is a different module and is left alone. An empty ``stem/`` directory is not a
    package, so it is left alone too.
    """
    stems: dict[str, set[str]] = {}
    for path in paths:
        parent = Path(path).parent.as_posix()
        if parent in ("", "."):
            continue
        stems.setdefault(parent, set()).add(_module_stem(Path(path).name))
    if not stems:
        return []
    hashed = set(paths)
    indexed = _head_index(root)
    if indexed is None:
        return ["could not list tracked files under core/"]
    files, _dirs = indexed
    untracked = _untracked(root, "core")
    if untracked is None:
        return ["could not list untracked files under core/"]
    parents = set(stems)
    found: list[str] = []
    seen: set[str] = set()

    def add(kind: str, rel: str) -> None:
        item = f"{kind} {rel}"
        if item not in seen:
            seen.add(item)
            found.append(item)

    def consider(rel: str) -> None:
        path = Path(rel)
        name = path.name
        parent = path.parent.as_posix()
        home = _pycache_home(rel)
        if home in parents:
            add("bytecode cache", rel)
            return
        # A hashed extension module is the file the import loads. It is not a
        # shadow of itself; its bytes are checked with the rest of the closure.
        if (parent in parents and rel not in hashed and any(
                name == stem + suffix
                for stem in stems[parent]
                for suffix in importlib.machinery.EXTENSION_SUFFIXES)):
            add("extension shadow", rel)
            return
        grand = path.parent.parent.as_posix()
        stem = path.parent.name
        if grand in parents and stem in stems[grand] and _is_init_filename(name):
            add("package shadow", rel)

    for rel in untracked:
        consider(rel)
    for rel in files:
        consider(rel)
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


def _dangling_emacs_lock(name: str, path: Path) -> bool:
    """True for a dangling ``.#*`` symlink (an Emacs lock, ``user@host.pid``)."""
    if not name.startswith(".#"):
        return False
    try:
        os.readlink(path)
    except OSError:
        return False
    return not os.path.exists(path)


def _benign_container(name: str) -> bool:
    """A dot-directory other than ``.git``, or a setuptools ``*.egg-info`` directory."""
    if name in (".", "..", ".git"):
        return False
    return name.startswith(".") or name.endswith(".egg-info")


def _dangerous_import_name(name: str) -> bool:
    return _has_import_suffix(name) or name.endswith(".pth") or name.endswith(".so")


def _has_any_file(path: Path) -> bool:
    """True when ``path`` contains a file, or a directory that cannot be listed.

    An unreadable directory is not empty: ``readdir`` cannot show what it hides.
    Symlinks are not followed.
    """
    blocked = False

    def onerror(_err: OSError) -> None:
        nonlocal blocked
        blocked = True

    for _dirpath, _dirnames, filenames in os.walk(path, followlinks=False, onerror=onerror):
        if filenames:
            return True
    return blocked


def _dangerous_files(root_rel: str, path: Path) -> list[str]:
    found: list[str] = []

    def onerror(err: OSError) -> None:
        found.append(f"{root_rel} cannot be listed ({err.strerror})")

    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False, onerror=onerror):
        rel_dir = Path(os.path.normpath(dirpath)).relative_to(path).as_posix()
        for name in filenames:
            if not _dangerous_import_name(name):
                continue
            child = name if rel_dir == "." else f"{rel_dir}/{name}"
            found.append(f"importable {root_rel}/{child}")
    return found


def _layout_problems(root: Path, pin_paths: list[str]) -> list[str]:
    """Symlinks, nested git entries, and importable directories under ``core/``.

    The walk does not follow symlinks and does not trust ``git ls-files`` output.
    A directory that cannot be listed fails closed (mode ``0111`` hides names from
    ``readdir``). Every symlink is refused, including one ``HEAD`` records, except a
    dangling Emacs ``.#*`` lock. An empty directory that lists cleanly is allowed.
    A dot-directory other than ``.git``, and a ``*.egg-info`` directory, are allowed
    when they hold no importable file.
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

    def onerror(err: OSError) -> None:
        raw = err.filename or ""
        try:
            rel = Path(os.path.normpath(raw)).relative_to(root_abs).as_posix()
        except ValueError:
            rel = raw or "core"
        problems.append(f"{rel} cannot be listed ({err.strerror})")

    for dirpath, dirnames, filenames in os.walk(core, followlinks=False, onerror=onerror):
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
                if _dangling_emacs_lock(name, full):
                    prune.add(name)
                    continue
                problems.append(f"symlink {rel}")
                prune.add(name)
                continue
            # An unchecked-hash or timestamp-matched .pyc runs under -B from any
            # directory, including one outside the import-search set.
            if (name in filenames and stat.S_ISREG(ent.st_mode) and rel not in tracked
                    and _is_bytecode(name)):
                problems.append(f"bytecode cache {rel}")
            if (parent_relevant and stat.S_ISDIR(ent.st_mode)
                    and rel not in tracked_dirs and rel not in tracked):
                if not _has_any_file(full):
                    pass
                elif _benign_container(name):
                    problems.extend(_dangerous_files(rel, full))
                else:
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
        elif len(under.parts) >= 2 and _is_init_filename(under.parts[1]):
            found.append(f"untracked top-level package {rel}")
    return found


def _ancestor_inits(path: str) -> list[str]:
    """Package ``__init__.py`` files above ``path``, under ``core/`` and not ``core`` itself.

    ``path`` is a pinned file or a pinned directory. A directory's own ``__init__.py``
    is not listed here; the directory seed includes that file so its imports are walked.
    """
    parts = Path(path).parts
    if len(parts) < 2 or parts[0] != "core":
        return []
    acc = ["core"]
    inits: list[str] = []
    for part in parts[1:-1]:
        acc.append(part)
        inits.append("/".join(acc + ["__init__.py"]))
    return inits


def _import_name(rel: str) -> str:
    """Import name of a ``core/`` module path. ``__init__.*`` names its package."""
    parts = Path(rel).parts
    if len(parts) < 2 or parts[0] != "core":
        return ""
    body = list(parts[1:])
    stem = _strip_loader_suffix(body[-1])
    if stem is None:
        return ""
    if stem == "__init__":
        body = body[:-1]
    else:
        body[-1] = stem
    if not body or any(part in ("", ".", "..") for part in body):
        return ""
    return ".".join(body)


def _pin_module_name(path: str) -> str:
    """Import name of a pinned file or of a pinned directory."""
    named = _import_name(path)
    if named:
        return named
    parts = Path(path).parts
    if len(parts) < 2 or parts[0] != "core":
        return ""
    if _strip_loader_suffix(parts[-1]) is not None:
        return ""
    if any(part in ("", ".", "..") for part in parts[1:]):
        return ""
    return ".".join(parts[1:])


def _package_of(rel: str) -> str:
    """Import package of a ``core/`` module path. ``__init__.py`` is its own package."""
    name = _import_name(rel)
    if not name:
        return ""
    if Path(rel).name in _INIT_NAMES or _strip_loader_suffix(Path(rel).name) == "__init__":
        return name
    return name.rpartition(".")[0]


def _from_parent(package: str, node: ast.ImportFrom) -> str:
    """Absolute module an ``ImportFrom`` loads, or ``""`` when the relative import is nonsense."""
    if node.level:
        parts = package.split(".") if package else []
        drop = node.level - 1
        if drop > len(parts):
            return ""
        base = parts[:len(parts) - drop] if drop else list(parts)
        if node.module:
            return ".".join([*base, *node.module.split(".")])
        return ".".join(base)
    return node.module or ""


def _static_import_names(package: str, node: ast.AST) -> tuple[list[str], str | None]:
    """Import targets in ``node``, and the package of ``import *`` when there is one.

    ``from . import name`` does not name the enclosing package again. ``*`` is not a
    module; the caller reads ``__all__`` for that package.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names], None
    if not isinstance(node, ast.ImportFrom):
        return [], None
    parent = _from_parent(package, node)
    found: list[str] = []
    star: str | None = None
    if node.level:
        if node.module and parent:
            found.append(parent)
    elif node.module:
        found.append(node.module)
    for alias in node.names:
        if alias.name == "*":
            if parent:
                star = parent
            continue
        if parent:
            found.append(f"{parent}.{alias.name}")
    return found, star


def _resolve_level_name(name: str, level: int, package: str) -> str | None:
    """Absolute name for ``__import__(name, level=level)`` inside ``package``.

    Same rule as ``importlib._bootstrap._resolve_name``: ``level`` leading dots
    climb ``package``, then ``name`` is appended.
    """
    if level <= 0:
        return name or None
    if not package or level > package.count(".") + 1:
        return None
    bits = package.rsplit(".", level - 1)
    if len(bits) < level:
        return None
    base = bits[0]
    if name:
        return f"{base}.{name}"
    return base or None


def _resolve_import_module_name(name: str, package: str | None) -> str | None:
    """Absolute name ``importlib.import_module(name, package)`` loads."""
    if not name.startswith("."):
        return name or None
    if not package:
        return None
    level = 0
    for character in name:
        if character != ".":
            break
        level += 1
    return _resolve_level_name(name[level:], level, package)


_UNKNOWN = object()
_NULL = object()
_EXEC_DEPTH = 8
_CALL_TAGS = {
    ("importlib", "import_module"): "import_module",
    ("importlib", "__import__"): "__import__",
    ("__import__",): "__import__",
    ("builtins", "__import__"): "__import__",
    ("builtins", "exec"): "exec",
    ("builtins", "compile"): "compile",
    ("exec",): "exec",
    ("compile",): "compile",
    ("importlib", "util", "spec_from_file_location"): "spec",
}
_PATH_ATTRS = ("path", "meta_path", "path_hooks")
_BYTECODE_DANGER = frozenset({
    "__import__", "exec", "compile", "import_module", "spec_from_file_location"})


def _keep_module(mod: str) -> bool:
    parts = mod.split(".")
    return mod.startswith("recursion_core") and all(part.isidentifier() for part in parts)


def _kw_get(positional: list, keywords: dict, index: int, name: str):
    if name in keywords:
        return keywords[name]
    if index < len(positional):
        return positional[index]
    return None


def _call_effects(tag: str, positional: list, keywords: dict, package: str):
    """``(modules, stars, dynamic, sources)`` for one import-shaped call.

    ``dynamic`` refuses the pin. ``sources`` are string literals passed to ``exec``
    or ``compile``, scanned as further source. A missing value is ``_UNKNOWN``.
    """
    if tag == "import_module":
        name = _kw_get(positional, keywords, 0, "name")
        if not isinstance(name, str):
            return [], [], True, []
        if name.startswith("."):
            package_arg = _kw_get(positional, keywords, 1, "package")
            if not isinstance(package_arg, str):
                return [], [], True, []
            resolved = _resolve_import_module_name(name, package_arg)
        else:
            resolved = name
        if not resolved:
            return [], [], True, []
        return [resolved], [], False, []
    if tag == "__import__":
        name = _kw_get(positional, keywords, 0, "name")
        fromlist = _kw_get(positional, keywords, 3, "fromlist")
        level = _kw_get(positional, keywords, 4, "level")
        if level is None:
            level = 0
        if (not isinstance(name, str) or isinstance(level, bool)
                or not isinstance(level, int)):
            return [], [], True, []
        resolved = _resolve_level_name(name, level, package) if level else (name or None)
        if not resolved:
            return [], [], True, []
        if fromlist is None:
            return [resolved], [], False, []
        if fromlist is _UNKNOWN or not isinstance(fromlist, (list, tuple)):
            return [], [], True, []
        if not all(isinstance(item, str) for item in fromlist):
            return [], [], True, []
        modules = [resolved]
        stars: list[str] = []
        if "*" in fromlist:
            stars.append(resolved)
        for item in fromlist:
            if item and item != "*":
                modules.append(f"{resolved}.{item}")
        return modules, stars, False, []
    if tag == "spec":
        name = _kw_get(positional, keywords, 0, "name")
        location = _kw_get(positional, keywords, 1, "location")
        if not isinstance(name, str):
            return [], [], True, []
        if location is not None and (location is _UNKNOWN or not isinstance(location, str)):
            return [], [], True, []
        return [name], [], False, []
    if tag in ("exec", "compile"):
        source = _kw_get(positional, keywords, 0, "source")
        if not isinstance(source, str):
            return [], [], True, []
        return [], [], False, [source]
    return [], [], False, []


def _string_seq(node: ast.AST | None) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names: list[str] = []
    for elt in node.elts:
        if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
            return None
        names.append(elt.value)
    return names


def _is_all_target(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "__all__"
    if isinstance(node, (ast.Tuple, ast.List)):
        return any(_is_all_target(elt) for elt in node.elts)
    if isinstance(node, ast.Starred):
        return _is_all_target(node.value)
    return False


def _literal_all(text: str) -> tuple[list[str] | None, bool]:
    """``(names, dynamic)`` from assignments to ``__all__``.

    No assignment is ``([], False)``. A non-literal assignment is ``(None, True)``.
    Several literal assignments are united, which is the fail-closed set.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None, True
    names: list[str] = []
    saw = False
    dynamic = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets: list[ast.AST] = list(node.targets)
            value: ast.AST | None = node.value
        elif isinstance(node, ast.AnnAssign):
            if node.value is None:
                continue
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.AugAssign):
            if _is_all_target(node.target):
                saw = True
                dynamic = True
            continue
        else:
            continue
        if not any(_is_all_target(target) for target in targets):
            continue
        saw = True
        seq = _string_seq(value) if len(targets) == 1 and isinstance(targets[0], ast.Name) else None
        if seq is None:
            dynamic = True
        else:
            names.extend(seq)
    if not saw:
        return [], False
    if dynamic:
        return None, True
    return names, False


class _SourceScan(ast.NodeVisitor):
    """Import closure and fail-closed reasons for one hashed source module."""

    def __init__(self, rel: str, depth: int, aliases: dict | None,
                 sys_names: set | None, path_aliases: dict | None,
                 dynamic: str = "full") -> None:
        self.rel = rel
        self.package = _package_of(rel)
        self.depth = depth
        # ``full`` records every dynamic call. ``module`` records only calls that run
        # on import. ``off`` still collects import names for the hash closure.
        self.dynamic = dynamic
        self.func_depth = 0
        self.modules: list[str] = []
        self.stars: list[str] = []
        self.top_modules: list[str] = []
        self.top_stars: list[str] = []
        self.problems: list[str] = []
        self.aliases: dict[str, tuple[str, ...]] = {
            "__import__": ("__import__",),
            "exec": ("exec",),
            "compile": ("compile",),
        }
        if aliases:
            self.aliases.update(aliases)
        self.sys_names = set(sys_names) if sys_names is not None else {"sys"}
        self.sys_names.add("sys")
        self.path_aliases = dict(path_aliases or {})

    def finish(self) -> tuple[list[str], list[str], list[str]]:
        modules = [mod for mod in self.modules if _keep_module(mod)]
        stars = [mod for mod in self.stars if _keep_module(mod)]
        return modules, stars, self.problems

    def _records_dynamic(self) -> bool:
        if self.dynamic == "off":
            return False
        if self.dynamic == "module" and self.func_depth:
            return False
        return True

    def _dyn(self) -> None:
        if self._records_dynamic():
            self.problems.append(f"{self.rel} dynamic import")

    def _mut(self, label: str) -> None:
        if self._records_dynamic():
            self.problems.append(f"{self.rel} mutates {label}")

    def _note_top(self, modules: list[str], stars: list[str]) -> None:
        if self.func_depth:
            return
        self.top_modules.extend(modules)
        self.top_stars.extend(stars)

    def collect_aliases(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = tuple(alias.name.split("."))
                    if not parts or any(part in ("", ".", "..") for part in parts):
                        continue
                    if alias.asname:
                        self.aliases[alias.asname] = parts
                    else:
                        self.aliases[parts[0]] = (parts[0],)
                    if parts == ("sys",):
                        self.sys_names.add(alias.asname or "sys")
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            base_name = _from_parent(self.package, node) if node.level else (node.module or "")
            if not base_name:
                continue
            base = tuple(base_name.split("."))
            for alias in node.names:
                if alias.name == "*":
                    self._star_aliases(base)
                    continue
                local = alias.asname or alias.name
                self.aliases[local] = base + (alias.name,)
                if base == ("sys",) and alias.name in _PATH_ATTRS:
                    self.path_aliases[local] = f"sys.{alias.name}"

    def _star_aliases(self, base: tuple[str, ...]) -> None:
        if base == ("importlib",):
            self.aliases.setdefault("import_module", ("importlib", "import_module"))
            self.aliases.setdefault("__import__", ("importlib", "__import__"))
        elif base == ("importlib", "util"):
            self.aliases.setdefault(
                "spec_from_file_location",
                ("importlib", "util", "spec_from_file_location"))
        elif base == ("sys",):
            for attr in _PATH_ATTRS:
                self.path_aliases.setdefault(attr, f"sys.{attr}")

    def _add_static(self, node: ast.AST) -> None:
        names, star = _static_import_names(self.package, node)
        self.modules.extend(names)
        stars = [star] if star else []
        self.stars.extend(stars)
        self._note_top(names, stars)

    def visit_Import(self, node: ast.Import) -> None:
        self._add_static(node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._add_static(node)
        self.generic_visit(node)

    def _dotted(self, node: ast.AST) -> tuple[str, ...] | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            if base is None:
                return None
            return base + (node.attr,)
        return None

    def _const(self, node: ast.AST):
        if isinstance(node, ast.Constant):
            return node.value
        # A literal tuple or list (``fromlist=('VALUE',)``) is still a constant.
        # A dict globals argument stays unknown; ``__import__`` does not read it.
        if isinstance(node, (ast.Tuple, ast.List)):
            values = []
            for elt in node.elts:
                if isinstance(elt, ast.Starred):
                    return _UNKNOWN
                value = self._const(elt)
                if value is _UNKNOWN:
                    return _UNKNOWN
                values.append(value)
            return tuple(values)
        return _UNKNOWN

    def _exec_source(self, source: str) -> None:
        if self.depth >= _EXEC_DEPTH:
            self._dyn()
            return
        child = self.dynamic
        if self.dynamic == "off" or (self.dynamic == "module" and self.func_depth):
            child = "off"
        modules, stars, problems, top_modules, top_stars = _scan_source(
            self.rel, source, depth=self.depth + 1, aliases=dict(self.aliases),
            sys_names=set(self.sys_names), path_aliases=dict(self.path_aliases),
            dynamic=child)
        self.modules.extend(modules)
        self.stars.extend(stars)
        if child != "off":
            self.problems.extend(problems)
        self._note_top(top_modules, top_stars)

    def _on_call(self, tag: str, node: ast.Call) -> None:
        if any(kw.arg is None for kw in node.keywords):
            self._dyn()
            return
        positional = [self._const(arg) for arg in node.args]
        keywords = {kw.arg: self._const(kw.value) for kw in node.keywords if kw.arg}
        modules, stars, dynamic, sources = _call_effects(tag, positional, keywords, self.package)
        if dynamic:
            self._dyn()
        self.modules.extend(modules)
        self.stars.extend(stars)
        self._note_top(modules, stars)
        for source in sources:
            self._exec_source(source)

    def _is_sys(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.sys_names

    def _path_expr(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            if node.id == "__path__":
                return "__path__"
            return self.path_aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr == "__path__":
                return "__path__"
            if node.attr in _PATH_ATTRS and self._is_sys(node.value):
                return f"sys.{node.attr}"
        return None

    def _mutate_target(self, target: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._mutate_target(elt)
            return
        if isinstance(target, ast.Starred):
            self._mutate_target(target.value)
            return
        label = self._path_expr(target)
        if label:
            self._mut(label)
            return
        if isinstance(target, ast.Subscript):
            label = self._path_expr(target.value)
            if label:
                self._mut(label)

    def visit_Call(self, node: ast.Call) -> None:
        tag = _CALL_TAGS.get(self._dotted(node.func) or ())
        if tag:
            self._on_call(tag, node)
        if isinstance(node.func, ast.Attribute):
            label = self._path_expr(node.func.value)
            if label:
                self._mut(label)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._mutate_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._mutate_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._mutate_target(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._mutate_target(target)
        self.generic_visit(node)

    def _visit_function(self, node: ast.AST) -> None:
        self.func_depth += 1
        self.generic_visit(node)
        self.func_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function(node)


def _scan_source(rel: str, text: str, *, depth: int = 0, aliases: dict | None = None,
                 sys_names: set | None = None, path_aliases: dict | None = None,
                 dynamic: str = "full"):
    """``(modules, star packages, problems, module-level modules, module-level stars)``.

    ``dynamic`` is ``full``, ``module``, or ``off``. Import names are collected either way.
    """
    if depth > _EXEC_DEPTH:
        problem = [f"{rel} dynamic import"] if dynamic != "off" else []
        return [], [], problem, [], []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        problem = [f"{rel} could not parse imports"] if dynamic != "off" else []
        return [], [], problem, [], []
    scan = _SourceScan(rel, depth, aliases, sys_names, path_aliases, dynamic)
    scan.collect_aliases(tree)
    scan.visit(tree)
    modules, stars, problems = scan.finish()
    return modules, stars, problems, scan.top_modules, scan.top_stars


def _pushes_null(ins: dis.Instruction) -> bool:
    return ins.opname in ("LOAD_GLOBAL", "LOAD_ATTR") and isinstance(ins.arg, int) and ins.arg & 1 == 1


def _path_label(item) -> str | None:
    """``__path__`` or ``sys.path`` / ``sys.meta_path`` / ``sys.path_hooks``, else None."""
    if isinstance(item, tuple) and item[0] == "n" and item[1] == "__path__":
        return "__path__"
    if (isinstance(item, tuple) and item[0] == "a" and item[2] in _PATH_ATTRS
            and isinstance(item[1], tuple) and item[1][0] == "n" and item[1][1] == "sys"):
        return f"sys.{item[2]}"
    return None


def _func_tag(item) -> str | None:
    if isinstance(item, tuple) and item[0] == "n" and item[1] in (
            "__import__", "exec", "compile", "import_module", "spec_from_file_location"):
        return "spec" if item[1] == "spec_from_file_location" else item[1]
    if isinstance(item, tuple) and item[0] == "a":
        owner, attr = item[1], item[2]
        if attr in ("import_module", "__import__", "spec_from_file_location"):
            return "spec" if attr == "spec_from_file_location" else attr
        if (attr in ("exec", "compile", "__import__") and isinstance(owner, tuple)
                and owner[0] == "n" and owner[1] == "builtins"):
            return attr
    return None


def _scan_code(code: types.CodeType, rel: str, package: str):
    """``(modules, stars, sources, problems, lost)`` from one code object, not its nested ones."""
    modules: list[str] = []
    stars: list[str] = []
    sources: list[str] = []
    problems: list[str] = []
    stack: list = []
    kw_names: tuple = ()
    lost = False

    def push(item) -> None:
        stack.append(item)

    def pop():
        if not stack:
            return _UNKNOWN
        return stack.pop()

    instrs = list(dis.get_instructions(code))
    for index, ins in enumerate(instrs):
        op = ins.opname
        if op in ("CACHE", "RESUME", "NOP", "PRECALL"):
            continue
        if op == "PUSH_NULL":
            push(_NULL)
        elif op == "LOAD_CONST":
            push(("c", ins.argval))
        elif op in ("LOAD_NAME", "LOAD_FAST", "LOAD_DEREF", "LOAD_GLOBAL"):
            if _pushes_null(ins):
                push(_NULL)
            push(("n", ins.argval))
        elif op == "LOAD_ATTR":
            owner = pop()
            if _pushes_null(ins):
                push(_NULL)
            push(("a", owner, ins.argval))
        elif op in ("STORE_NAME", "STORE_GLOBAL"):
            pop()
            if ins.argval == "__path__":
                problems.append(f"{rel} mutates __path__")
        elif op in ("STORE_FAST", "STORE_DEREF", "POP_TOP", "RETURN_VALUE"):
            pop()
        elif op == "STORE_ATTR":
            owner = pop()
            pop()
            if ins.argval == "__path__":
                problems.append(f"{rel} mutates __path__")
            elif ins.argval in _PATH_ATTRS and isinstance(owner, tuple) and owner[0] == "n" and owner[1] == "sys":
                problems.append(f"{rel} mutates sys.{ins.argval}")
        elif op in ("STORE_SUBSCR",):
            _key = pop()
            container = pop()
            pop()
            label = _path_label(container)
            if label:
                problems.append(f"{rel} mutates {label}")
        elif op == "DELETE_SUBSCR":
            pop()
            label = _path_label(pop())
            if label:
                problems.append(f"{rel} mutates {label}")
        elif op in ("BUILD_LIST", "BUILD_TUPLE", "BUILD_SET"):
            for _ in range(ins.arg or 0):
                pop()
            push(_UNKNOWN)
        elif op in ("BINARY_OP", "COMPARE_OP"):
            pop()
            pop()
            push(_UNKNOWN)
        elif op in ("RETURN_CONST",):
            pass
        elif op == "KW_NAMES":
            kw_names = tuple(ins.argval or ())
        elif op == "IMPORT_NAME":
            name = ins.argval if isinstance(ins.argval, str) else None
            ok = (index >= 2 and instrs[index - 2].opname == "LOAD_CONST"
                  and instrs[index - 1].opname == "LOAD_CONST")
            level = instrs[index - 2].argval if ok else None
            fromlist = instrs[index - 1].argval if ok else None
            pop()
            pop()
            push(_UNKNOWN)
            good = (ok and name is not None and isinstance(level, int)
                    and not isinstance(level, bool)
                    and (fromlist is None or (
                        isinstance(fromlist, tuple) and all(isinstance(item, str) for item in fromlist))))
            if not good:
                problems.append(f"{rel} dynamic import")
                continue
            resolved = _resolve_level_name(name, level, package) if level else (name or None)
            if not resolved:
                problems.append(f"{rel} dynamic import")
                continue
            modules.append(resolved)
            if fromlist:
                if "*" in fromlist:
                    stars.append(resolved)
                for item in fromlist:
                    if item and item != "*":
                        modules.append(f"{resolved}.{item}")
        elif op == "IMPORT_FROM":
            push(_UNKNOWN)
        elif op == "CALL_INTRINSIC_1":
            pop()
            push(_UNKNOWN)
        elif op == "CALL":
            argc = ins.arg or 0
            args = [pop() for _ in range(argc)]
            args.reverse()
            func = pop()
            if stack and stack[-1] is _NULL:
                pop()
            names = kw_names
            kw_names = ()
            if len(names) > len(args):
                lost = True
                problems.append(f"{rel} dynamic import")
                continue
            positional = args[:-len(names)] if names else args
            keywords = {}
            for key, item in zip(names, args[len(args) - len(names):]):
                keywords[key] = item[1] if isinstance(item, tuple) and item[0] == "c" else _UNKNOWN
            pos_values = [item[1] if isinstance(item, tuple) and item[0] == "c" else _UNKNOWN
                          for item in positional]
            tag = _func_tag(func)
            if isinstance(func, tuple) and func[0] == "a":
                label = _path_label(func[1])
                if label:
                    problems.append(f"{rel} mutates {label}")
            if tag is None:
                continue
            got, got_stars, dynamic, got_sources = _call_effects(tag, pos_values, keywords, package)
            if dynamic:
                problems.append(f"{rel} dynamic import")
            modules.extend(got)
            stars.extend(got_stars)
            sources.extend(got_sources)
        else:
            lost = True
    if lost and _BYTECODE_DANGER.intersection(code.co_names):
        problems.append(f"{rel} dynamic import")
    return modules, stars, sources, problems, lost


def _load_pyc_code(path: Path) -> types.CodeType | None:
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 16:
        return None
    try:
        code = marshal.loads(data[16:])
    except (EOFError, ValueError, TypeError):
        return None
    if not isinstance(code, types.CodeType):
        return None
    return code


def _bytecode_top_imports(path: Path, rel: str) -> tuple[list[str], list[str]]:
    """Module-level import names in a sourceless ``.pyc``, ignoring nested code objects."""
    code = _load_pyc_code(path)
    if code is None:
        return [], []
    modules, stars, _sources, _problems, _lost = _scan_code(code, rel, _package_of(rel))
    return ([mod for mod in modules if _keep_module(mod)],
            [mod for mod in stars if _keep_module(mod)])


def _scan_bytecode(path: Path, rel: str, dynamic: str = "full"):
    """``(modules, star packages, problems)`` for a sourceless ``.pyc``.

    The code object is marshalled after the 16-byte header and is not executed.
    Nested code objects in ``co_consts`` are walked the same way. A header or
    marshal failure refuses the pin. ``dynamic`` selects which of those code
    objects may refuse the pin; import names are collected either way.
    """
    package = _package_of(rel)
    code = _load_pyc_code(path)
    if code is None:
        problem = [f"{rel} could not read bytecode imports"] if dynamic != "off" else []
        return [], [], problem
    modules: list[str] = []
    stars: list[str] = []
    problems: list[str] = []

    def walk(block: types.CodeType, level: int) -> None:
        got, got_stars, sources, probs, _lost = _scan_code(block, rel, package)
        modules.extend(got)
        stars.extend(got_stars)
        record = dynamic == "full" or (dynamic == "module" and level == 0)
        if record:
            problems.extend(probs)
        child = dynamic if record else "off"
        for source in sources:
            nested_m, nested_s, nested_p, _top_m, _top_s = _scan_source(
                rel, source, dynamic=child)
            modules.extend(nested_m)
            stars.extend(nested_s)
            if child != "off":
                problems.extend(nested_p)
        for const in block.co_consts:
            if isinstance(const, types.CodeType):
                walk(const, level + 1)
            elif isinstance(const, tuple):
                for item in const:
                    if isinstance(item, types.CodeType):
                        walk(item, level + 1)

    walk(code, 0)
    return ([mod for mod in modules if _keep_module(mod)],
            [mod for mod in stars if _keep_module(mod)],
            problems)


def _star_submodules(root: Path, files: dict[str, tuple[str, str]], mod: str):
    """Submodules named by a literal ``__all__``, or a reason ``__all__`` is not literal."""
    if not _keep_module(mod):
        return [], None
    chosen = _select_import(root, files, mod)
    if chosen is None:
        return [], None
    text = _read_module_text(root / chosen)
    if text is None:
        return [], "star import __all__ is not literal"
    names, dynamic = _literal_all(text)
    if dynamic or names is None:
        return [], "star import __all__ is not literal"
    found: list[str] = []
    for name in names:
        if not name.isidentifier():
            continue
        sub = f"{mod}.{name}"
        if _select_import(root, files, sub) is not None:
            found.append(sub)
    return found, None


def _tracked_regular(files: dict[str, tuple[str, str]], rel: str) -> bool:
    meta = files.get(rel)
    return meta is not None and meta[0] in ("100644", "100755")


def _select_import(root: Path, files: dict[str, tuple[str, str]], mod: str) -> str | None:
    """Path FileFinder would load for ``mod``, or None for a namespace with no shadow file.

    Order matches FileFinder: ``__init__`` plus each loader suffix, then the module file
    plus each loader suffix. Extension beats source, and source beats sourceless bytecode.
    A sibling ``ns.py`` or ``ns/__init__.so`` therefore wins over a namespace directory.
    """
    if not mod.startswith("recursion_core"):
        return None
    parts = mod.split(".")
    if any(part in ("", ".", "..") for part in parts):
        return None
    base = "core/" + "/".join(parts)
    candidates = [f"{base}/__init__{suffix}" for suffix in _LOADER_SUFFIXES]
    candidates += [f"{base}{suffix}" for suffix in _LOADER_SUFFIXES]
    for cand in candidates:
        if _exists_regular_or_link(root / cand):
            return cand
    for cand in candidates:
        if _tracked_regular(files, cand):
            return cand
    return None


def _resolved_paths(root: Path, files: dict[str, tuple[str, str]], mod: str) -> list[str]:
    """The file FileFinder loads for ``mod`` and for each of its parent packages."""
    parts = mod.split(".")
    if not mod.startswith("recursion_core") or any(part in ("", ".", "..") for part in parts):
        return []
    found: list[str] = []
    for index in range(1, len(parts) + 1):
        chosen = _select_import(root, files, ".".join(parts[:index]))
        if chosen is not None and chosen not in found:
            found.append(chosen)
    return found


def _source_seeds(path: str, files: dict[str, tuple[str, str]]) -> list[str]:
    """Tracked source files the import walk starts from.

    A file pin starts at that file. A directory pin starts at every ``.py`` in the
    tree, including its own ``__init__.py``. The directory path is not a module file.
    """
    if _tracked_regular(files, path):
        return [path]
    prefix = path + "/"
    seeds: list[str] = []
    for rel, meta in files.items():
        if meta[0] not in ("100644", "100755") or not rel.startswith(prefix):
            continue
        if Path(rel).name.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES)):
            seeds.append(rel)
    return seeds


def _exists_regular_or_link(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)


def _read_module_text(path: Path) -> str | None:
    """Source text of a ``.py`` file. Sourceless and extension files are not decoded."""
    if not path.name.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES)):
        return None
    try:
        st = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _pin_entries(path: str, files: dict[str, tuple[str, str]]) -> list[str]:
    """Files importing this pin executes before any of its own imports.

    A file pin's entry is that file. A directory pin's entry is the package
    ``__init__.py`` the driver imports, not every module that happens to sit in the tree.
    """
    if _tracked_regular(files, path):
        return [path]
    init = path.rstrip("/") + "/__init__.py"
    if _tracked_regular(files, init):
        return [init]
    return []


def _dynamic_mode(rel: str, entries: set[str], executed: set[str]) -> str:
    """How much dynamic code in ``rel`` may refuse the pin before the run."""
    if rel not in executed:
        return "off"
    if rel in entries or _is_bytecode(Path(rel).name):
        return "full"
    return "module"


def _executed_files(root: Path, path: str, files: dict[str, tuple[str, str]]) -> tuple[set[str], set[str]]:
    """``(entry files, files importing the entry actually loads at module level)``.

    Function bodies are not followed. ``daemon/__main__.py`` and a test helper the
    package never imports stay out of the set, so dynamic code there is not an early
    refusal. ``plugin_security/__init__.py`` does import ``loader.py``, so that module
    is in the set, but only its module-level statements are.
    """
    entries = set(_pin_entries(path, files))
    queue = [*entries, *_ancestor_inits(path)]
    pin_mod = _pin_module_name(path)
    if pin_mod:
        queue.extend(_resolved_paths(root, files, pin_mod))
    executed: set[str] = set()
    seen: set[str] = set()
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        executed.add(rel)
        text = _read_module_text(root / rel)
        if text is not None:
            _modules, _stars, _probs, top_modules, top_stars = _scan_source(
                rel, text, dynamic="off")
            follow = [mod for mod in top_modules if _keep_module(mod)]
            star_mods = [mod for mod in top_stars if _keep_module(mod)]
        elif _is_bytecode(Path(rel).name):
            follow, star_mods = _bytecode_top_imports(root / rel, rel)
        else:
            continue
        for mod in follow:
            for found in _resolved_paths(root, files, mod):
                if found not in seen:
                    queue.append(found)
        for mod in star_mods:
            subs, _problem = _star_submodules(root, files, mod)
            for sub in subs:
                for found in _resolved_paths(root, files, sub):
                    if found not in seen:
                        queue.append(found)
    return entries, executed


def _file_pin_dependency_problems(root: Path, path: str) -> tuple[list[str], list[str]]:
    """Ancestor package inits, and the tracked modules they or the pin import, must match HEAD.

    Importing a pinned file or a pinned directory runs those ``__init__.py`` files first.
    A directory pin also walks every tracked source file in the directory, so an import
    in the package body is part of the closure. FileFinder order applies: a namespace
    portion loses to a sibling module or extension and to ``__init__`` plus any loader
    suffix. A sourceless ``.pyc`` the import resolves to is hashed and its imports are
    walked; an extension file is hashed and its imports are not. Only ``recursion_core``
    names are followed. The second list is every tracked module path in that closure,
    so a shadow check can cover parents that are not import-search directories.
    """
    indexed = _head_index(root)
    if indexed is None:
        return ["could not list tracked files under core/"], []
    files, _dirs = indexed
    entries, executed = _executed_files(root, path, files)
    problems: list[str] = []
    queue = [*_source_seeds(path, files), *_ancestor_inits(path)]
    pin_mod = _pin_module_name(path)
    if pin_mod:
        queue.extend(_resolved_paths(root, files, pin_mod))
    seen: set[str] = set()
    extra: list[tuple[str, str]] = []
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        meta = files.get(rel)
        if meta is None or meta[0] not in ("100644", "100755"):
            if rel != path and _exists_regular_or_link(root / rel):
                problems.append(f"untracked import {rel}")
            continue
        if rel != path:
            extra.append((rel, meta[1]))
        own = _import_name(rel)
        if own:
            queue.extend(p for p in _resolved_paths(root, files, own) if p not in seen)
        mode = _dynamic_mode(rel, entries, executed)
        text = _read_module_text(root / rel)
        if text is not None:
            modules, stars, probs, _top_m, _top_s = _scan_source(rel, text, dynamic=mode)
        elif _is_bytecode(Path(rel).name):
            modules, stars, probs = _scan_bytecode(root / rel, rel, dynamic=mode)
        else:
            modules, stars, probs = [], [], []
        problems.extend(probs)
        for mod in modules:
            queue.extend(p for p in _resolved_paths(root, files, mod) if p not in seen)
        for mod in stars:
            subs, problem = _star_submodules(root, files, mod)
            if problem and mode != "off":
                problems.append(f"{rel} {problem}")
            for sub in subs:
                queue.extend(p for p in _resolved_paths(root, files, sub) if p not in seen)
    closure: list[str] = []
    if path in files and files[path][0] in ("100644", "100755"):
        closure.append(path)
    for rel, _oid in extra:
        if rel not in closure:
            closure.append(rel)
    names: list[str] = []
    oids: list[str] = []
    for rel, oid in extra:
        chain = _parent_chain_problem(root, rel)
        if chain:
            problems.append(chain)
            continue
        file = root / rel
        if not file.is_file() or file.is_symlink():
            problems.append(f"{rel} missing or is a symlink")
            continue
        names.append(rel)
        oids.append(oid)
    if not names:
        return problems, closure
    out = _git(root, "hash-object", "--no-filters", "--", *names)
    if out is None or out.returncode != 0 or len(out.stdout.splitlines()) != len(names):
        problems.append("could not hash imported worktree bytes")
        return problems, closure
    for rel, oid, got in zip(names, oids, out.stdout.splitlines()):
        if got != oid:
            problems.append(f"{rel} worktree bytes differ")
    return problems, closure


def _untracked_bytecode_caches(root: Path, modules: list[str]) -> list[str]:
    """Untracked ``cache_from_source`` files for hashed source modules.

    Python opens that path by name under ``-B``. ``git ls-files`` and ``os.walk``
    both miss it when the ``__pycache__`` directory cannot be listed.
    """
    indexed = _head_index(root)
    if indexed is None:
        return ["could not list tracked files under core/"]
    tracked, _dirs = indexed
    root_abs = Path(os.path.normpath(os.path.abspath(root)))
    found: list[str] = []
    seen: set[str] = set()
    for rel in modules:
        if not rel.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES)):
            continue
        src = root_abs / rel
        try:
            cache = importlib.util.cache_from_source(os.fspath(src))
        except (OSError, ValueError, NotImplementedError):
            found.append(f"bytecode cache for {rel} cannot be named")
            continue
        cache_path = Path(cache)
        try:
            st = cache_path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            found.append(f"bytecode cache for {rel} cannot be statted ({exc.strerror})")
            continue
        try:
            crel = Path(os.path.normpath(cache_path)).relative_to(root_abs).as_posix()
        except ValueError:
            found.append(f"bytecode cache for {rel} is outside the checkout")
            continue
        if crel in seen:
            continue
        seen.add(crel)
        if stat.S_ISLNK(st.st_mode):
            found.append(f"symlink {crel}")
        elif not stat.S_ISREG(st.st_mode) or crel not in tracked:
            found.append(f"bytecode cache {crel}")
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
    dep, closure = _file_pin_dependency_problems(root, path)
    problems.extend(dep)
    problems.extend(_file_pin_shadows(root, closure))
    problems.extend(_untracked_bytecode_caches(
        root, [name for name, _blob in blobs] + list(closure)))
    if not (len(blobs) == 1 and blobs[0][0] == path):
        problems.extend(_directory_pin_shadows(root, path, blobs))
    return problems


_FILE_LOADERS = (
    importlib.machinery.SourceFileLoader,
    importlib.machinery.SourcelessFileLoader,
    importlib.machinery.ExtensionFileLoader,
)
_BENIGN_LOADERS = frozenset({"BuiltinImporter", "FrozenImporter", "NamespaceLoader"})
_CODE_OPEN_SUFFIXES = tuple(
    list(importlib.machinery.SOURCE_SUFFIXES)
    + list(importlib.machinery.BYTECODE_SUFFIXES)
    + list(importlib.machinery.EXTENSION_SUFFIXES)
    + [".pth", ".zip"])


def _same_root(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == Path(right).resolve()
    except OSError:
        return False


def _rel_under(root: Path, filename: str) -> str | None:
    """Path of ``filename`` relative to ``root``, or None when it is not inside.

    Lexical only: this runs from the audit hook, which must not stat or open.
    """
    if not filename or filename.startswith("<"):
        return None
    if ".zip" + os.sep in filename or filename.endswith(".zip"):
        return None
    try:
        path = Path(os.path.normpath(filename))
        if not path.is_absolute():
            return None
        rel = path.relative_to(os.path.normpath(root))
    except ValueError:
        return None
    if not rel.parts or ".." in rel.parts:
        return None
    return rel.as_posix()


def _stdlib_file(filename: str) -> bool:
    if not filename or filename.startswith("<frozen") or filename.startswith("<builtin"):
        return True
    if filename.startswith("<"):
        return False
    try:
        path = Path(filename).resolve()
    except OSError:
        return False
    prefixes: list[Path] = []
    for key in ("stdlib", "platstdlib"):
        value = sysconfig.get_path(key) or ""
        if value:
            try:
                prefixes.append(Path(value).resolve())
            except OSError:
                continue
    try:
        prefixes.append(Path(sys.base_prefix).resolve())
    except OSError:
        pass
    for prefix in prefixes:
        try:
            path.relative_to(prefix)
            return True
        except ValueError:
            continue
    return False


def _path_key(seq) -> tuple:
    keyed: list[str] = []
    for item in seq:
        if isinstance(item, (str, bytes, os.PathLike)):
            keyed.append(os.path.normpath(os.fspath(item)))
        else:
            keyed.append(f"id:{id(item)}")
    return tuple(keyed)


def _id_key(seq) -> tuple:
    return tuple(id(item) for item in seq)


class _Execution:
    """What one armed process has imported, compiled, or opened since the snapshot."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = _path_key(sys.path)
        self.meta = _id_key(sys.meta_path)
        self.hooks = _id_key(sys.path_hooks)
        self.modules = set(sys.modules)
        self.files: list[str] = []
        self.opens: list[str] = []
        self.strings: list[str | None] = []
        self.bound: list[tuple[str, str | None]] = []
        self.path_changed = False
        self.meta_changed = False
        self.hooks_changed = False
        self.dll = False
        self.notes: list[str] = []


_state: _Execution | None = None
_hook_installed = False
_busy = False


def _caller_under_tree() -> bool:
    """True when the first frame outside the hook and importlib lives in the pinned tree."""
    if _state is None:
        return False
    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename
        name = frame.f_code.co_name
        if (name in ("_audit", "_record", "_caller_under_tree")
                or filename.startswith("<frozen importlib")
                or name == "_call_with_frames_removed"):
            frame = frame.f_back
            continue
        return _rel_under(_state.root, filename) is not None
    return False


def _record(event: str, args) -> None:
    state = _state
    if state is None:
        return
    if event == "import" and len(args) >= 5:
        # Some internal imports pass None for the path lists. That is not a mutation.
        if args[2] is not None and _path_key(args[2]) != state.path:
            state.path_changed = True
        if args[3] is not None and _id_key(args[3]) != state.meta:
            state.meta_changed = True
        if args[4] is not None and _id_key(args[4]) != state.hooks:
            state.hooks_changed = True
        filename = args[1]
        if isinstance(filename, str):
            state.files.append(filename)
        return
    if event == "compile" and len(args) >= 2 and _caller_under_tree():
        source, filename = args[0], args[1]
        if isinstance(source, bytes):
            text: str | None = source.decode("utf-8", "replace")
        elif isinstance(source, str):
            text = source
        else:
            text = None
        if isinstance(filename, str) and not filename.startswith("<"):
            state.files.append(filename)
            state.bound.append((filename, text))
        else:
            state.strings.append(text)
        return
    if event == "exec" and args:
        code = args[0]
        filename = getattr(code, "co_filename", None)
        if isinstance(filename, str) and not filename.startswith("<"):
            state.files.append(filename)
        elif _caller_under_tree() and isinstance(filename, str) and filename.startswith("<"):
            # ``exec`` of a code object compiled earlier still carries ``<string>``.
            # The compile event recorded the text when this process compiled it.
            if not isinstance(code, types.CodeType):
                state.strings.append(None)
        return
    if event == "open" and args and isinstance(args[0], str):
        mode = args[1] if len(args) > 1 else "r"
        if isinstance(mode, str) and mode[:1] in ("w", "a", "x"):
            return
        path = args[0]
        if path.endswith(_CODE_OPEN_SUFFIXES) or ".zip" + os.sep in path:
            state.opens.append(path)
        return
    if event == "os.add_dll_directory":
        state.dll = True


def _audit(event, args) -> None:
    global _busy
    if _busy or _state is None:
        return
    _busy = True
    try:
        _record(event, args)
    except Exception as exc:
        if _state is not None:
            _state.notes.append(f"audit hook failed on {event} ({type(exc).__name__}: {exc})")
    finally:
        _busy = False


def arm_execution_pin(root: Path | str) -> None:
    """Start recording executed code. Call this before importing the pinned tree.

    The snapshot is the process as it is now, so the harness may already have put
    ``tree/core`` on ``sys.path``. Later edits of that list are part of the run.
    """
    global _state, _hook_installed
    _state = _Execution(Path(root))
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True


def env_for_root(root: Path | str) -> str | None:
    """Pin env whose value is this checkout, or None when the variable is unset."""
    try:
        resolved = Path(root).resolve()
    except OSError:
        return None
    for env in PINS:
        value = os.environ.get(env)
        if not value:
            continue
        try:
            if Path(value).resolve() == resolved:
                return env
        except OSError:
            continue
    return None


def _blob_problem(root: Path, rel: str) -> str | None:
    """None when ``rel`` is a committed blob and the worktree bytes match it."""
    head = _git_text(root, "rev-parse", f"HEAD:{rel}")
    if not head:
        return f"executed {rel} is not a committed blob"
    out = _git(root, "hash-object", "--no-filters", "--", rel)
    if out is None or out.returncode != 0 or not out.stdout.strip():
        return f"executed {rel} could not be hashed"
    if out.stdout.strip() != head:
        return f"executed {rel} worktree bytes differ"
    return None


def _file_text(root: Path, rel: str) -> str | None:
    file = root / rel
    try:
        if not file.is_file() or file.is_symlink():
            return None
        return file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _hashed_texts(root: Path) -> set[str]:
    """Decoded text of tracked ``core/`` sources whose worktree bytes match HEAD."""
    blobs = _tracked_blobs(root, "core")
    if not blobs:
        return set()
    names = [rel for rel, _oid in blobs if rel.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES))]
    oids = [oid for rel, oid in blobs if rel.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES))]
    texts: set[str] = set()
    for start in range(0, len(names), 64):
        texts.update(_texts_for(root, names[start:start + 64], oids[start:start + 64]))
    return texts


def _texts_for(root: Path, names: list[str], oids: list[str]) -> set[str]:
    out = _git(root, "hash-object", "--no-filters", "--", *names)
    if out is None or out.returncode != 0:
        return set()
    got = out.stdout.splitlines()
    texts: set[str] = set()
    if len(got) != len(names):
        return set()
    for rel, oid, hashed in zip(names, oids, got):
        if hashed != oid:
            continue
        text = _file_text(root, rel)
        if text is not None:
            texts.add(text)
    return texts


def _loader_name(loader: object) -> str:
    """Class name of a loader instance, or the class's own name when the spec stores the class."""
    if loader is None:
        return ""
    if isinstance(loader, type):
        return loader.__name__
    return type(loader).__name__


def _package_path_problem(root: Path, module) -> str | None:
    origin = getattr(module, "__file__", None)
    pkg_path = getattr(module, "__path__", None)
    if not isinstance(origin, str) or pkg_path is None:
        return None
    if _rel_under(root, origin) is None:
        return None
    if Path(origin).name not in _INIT_NAMES and not Path(origin).name.startswith("__init__."):
        return None
    expected = os.path.normpath(str(Path(origin).resolve().parent))
    found: list[str] = []
    try:
        entries = list(pkg_path)
    except TypeError:
        return f"{getattr(module, '__name__', origin)} mutates __path__"
    for entry in entries:
        try:
            found.append(os.path.normpath(str(Path(os.fspath(entry)).resolve())))
        except (OSError, TypeError, ValueError):
            return f"{getattr(module, '__name__', origin)} mutates __path__"
    if found != [expected]:
        return f"{getattr(module, '__name__', origin)} mutates __path__"
    return None


def execution_problems() -> list[str]:
    """Reasons the armed run executed code that is not the hashed tree.

    Empty when nothing has been armed, or when every executed source matches a blob
    and the path lists are the ones snapshotted at ``arm_execution_pin``.
    """
    global _busy
    state = _state
    if state is None:
        return []
    _busy = True
    try:
        return _execution_problems(state)
    finally:
        _busy = False


def _execution_problems(state: _Execution) -> list[str]:
    root = state.root
    problems: list[str] = list(state.notes)
    if state.path_changed or _path_key(sys.path) != state.path:
        problems.append("mutates sys.path")
    if state.meta_changed or _id_key(sys.meta_path) != state.meta:
        problems.append("mutates sys.meta_path")
    if state.hooks_changed or _id_key(sys.path_hooks) != state.hooks:
        problems.append("mutates sys.path_hooks")
    if state.dll:
        problems.append("mutates os.add_dll_directory")
    texts = _hashed_texts(root) if state.strings or state.bound else set()
    for text in state.strings:
        if text is None or text not in texts:
            problems.append("untraced exec of a string")
    for filename, text in state.bound:
        rel = _rel_under(root, filename) if isinstance(filename, str) else None
        if rel is None or text is None:
            problems.append("untraced exec of a string")
            continue
        file_text = _file_text(root, rel)
        if file_text is None or text != file_text or file_text not in texts:
            problems.append(f"executed {rel} does not match the hashed source")
    seen_files: set[str] = set()
    for filename in [*state.files, *state.opens]:
        if not isinstance(filename, str) or filename in seen_files:
            continue
        seen_files.add(filename)
        if filename.startswith("<") or _stdlib_file(filename):
            continue
        # ``open`` is audited before the call, including reads of a cache that is not there.
        if filename in state.opens and not os.path.isfile(filename):
            continue
        suffix_code = filename.endswith(_CODE_OPEN_SUFFIXES) or ".zip" + os.sep in filename
        rel = _rel_under(root, filename)
        if rel is None:
            if suffix_code or filename.endswith(".pth") or ".zip" in filename:
                problems.append(f"executed {filename} is not a hashed file")
            continue
        if suffix_code or rel.endswith(tuple(importlib.machinery.SOURCE_SUFFIXES)):
            problem = _blob_problem(root, rel)
            if problem:
                problems.append(problem)
    for name, module in list(sys.modules.items()):
        if name in state.modules:
            continue
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None) if spec is not None else None
        origin = None
        if spec is not None:
            origin = getattr(spec, "origin", None)
        if not isinstance(origin, str):
            origin = getattr(module, "__file__", None)
        loader_name = _loader_name(loader)
        if loader is not None and not isinstance(loader, _FILE_LOADERS) and loader_name not in _BENIGN_LOADERS:
            problems.append(f"non-file loader {loader_name} for {name}")
        if isinstance(origin, str) and origin in ("built-in", "frozen"):
            continue
        if isinstance(origin, str) and not origin.startswith("<") and not _stdlib_file(origin):
            rel = _rel_under(root, origin)
            if rel is None:
                problems.append(f"executed {name} from {origin} is not a hashed file")
            else:
                problem = _blob_problem(root, rel)
                if problem:
                    problems.append(problem)
        path_problem = _package_path_problem(root, module)
        if path_problem:
            problems.append(path_problem)
    return _dedupe(problems)


def pin_status(env: str, root: Path) -> tuple[bool, str]:
    """``(ok, message)`` for the checkout ``root`` named by ``env``.

    The static scan can refuse before anything runs. When this process armed an
    execution pin for ``root``, the code that has actually run is part of the
    decision too.
    """
    pin = PINS[env]
    head = _git_text(root, "rev-parse", "HEAD")
    if head is None:
        return False, f"{env}={root} is not a git checkout; cannot check it against the pin {pin['head'][:8]}"
    problems = [f"{path}: {reason}" for path, oid in pin["objects"].items()
                for reason in _path_problems(root, path, oid)]
    problems.extend(_core_path_shadows(root))
    problems.extend(_layout_problems(root, list(pin["objects"])))
    if _state is not None and _same_root(_state.root, root):
        problems.extend(execution_problems())
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
