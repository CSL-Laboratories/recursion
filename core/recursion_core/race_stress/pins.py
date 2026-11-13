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

When the import resolves to a sourceless ``.pyc`` or an extension file, those worktree
bytes are hashed with ``git hash-object --no-filters`` and the file is not executed. That
is the fail-closed choice: a clean match stays pinned, and a changed, missing, or untracked
file is refused. Imports inside a sourceless or extension file are not walked. A tracked
``.pyc`` beside a same-stem ``.py`` is not the loaded module (source is tried first) and
stays allowed.

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

Not checked here: ``sitecustomize`` reached through ``PYTHONPATH`` when the process is not
isolated (``-B`` does not skip it); the ``PATH`` fallback when ``/usr/bin/git`` is absent;
a ``__spec__`` whose paths still name the pinned file, which ``verify_loaded`` accepts;
launching a driver as a plain script instead of the isolated runner.

Anything else skips with a message naming both heads, instead of measuring code nobody pinned.
Set ``RACE_STRESS_UNPINNED=1`` to run against an unpinned tree anyway (the run then reports
which tree it measured).

Re-pin by updating ``head`` and ``objects`` after re-running the suite on the new head:
``git -C <tree> rev-parse HEAD HEAD:<path>`` for each path.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
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


def _imported_modules(rel: str, text: str) -> list[str]:
    """``recursion_core`` modules named by import statements in ``text``."""
    package = _package_of(rel)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            parts = package.split(".") if package else []
            drop = node.level - 1
            if drop > len(parts):
                continue
            base = parts[:len(parts) - drop] if drop else list(parts)
            if node.module:
                parent = ".".join([*base, *node.module.split(".")])
                found.append(parent)
            else:
                parent = ".".join(base)
            # ``from pkg import name`` loads ``pkg.name`` when that submodule exists.
            found.extend(f"{parent}.{alias.name}" for alias in node.names if parent)
        elif node.module:
            found.append(node.module)
            found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return [mod for mod in found if mod.startswith("recursion_core")]


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


def _file_pin_dependency_problems(root: Path, path: str) -> tuple[list[str], list[str]]:
    """Ancestor package inits, and the tracked modules they or the pin import, must match HEAD.

    Importing a pinned file or a pinned directory runs those ``__init__.py`` files first.
    A directory pin also walks every tracked source file in the directory, so an import
    in the package body is part of the closure. FileFinder order applies: a namespace
    portion loses to a sibling module or extension and to ``__init__`` plus any loader
    suffix, and a sourceless or extension file the import resolves to is hashed rather
    than executed. Only ``recursion_core`` names are followed. The second list is every
    tracked module path in that closure, so a shadow check can cover parents that are
    not import-search directories.
    """
    indexed = _head_index(root)
    if indexed is None:
        return ["could not list tracked files under core/"], []
    files, _dirs = indexed
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
        text = _read_module_text(root / rel)
        if text is None:
            continue
        for mod in _imported_modules(rel, text):
            queue.extend(p for p in _resolved_paths(root, files, mod) if p not in seen)
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
