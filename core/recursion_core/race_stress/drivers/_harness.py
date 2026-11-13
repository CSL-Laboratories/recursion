"""Shared stress harness for the race_stress drivers (CSL-801). Standard library only.

Each driver runs as its own script so it can put a measured tree's ``core/`` first on
``sys.path`` without clashing with this checkout's ``recursion_core``.
"""

from __future__ import annotations

import sys


def _prefer_stdlib_over_drivers() -> None:
    """Keep this file's directory on ``sys.path`` after the standard library.

    A script launch puts ``drivers/`` first. An ``-I`` bootstrap that inserts it
    there makes ``import json`` load ``drivers/json.py``. ``sys`` is a built-in,
    so this runs before any other import can see that entry.
    """
    file = __file__
    drivers = ""
    for sep in ("/", "\\"):
        cut = file.rfind(sep)
        if cut > 0:
            drivers = file[:cut]
            break
    if not drivers:
        return
    sys.path[:] = [entry for entry in sys.path if entry != drivers]
    sys.path.append(drivers)


_prefer_stdlib_over_drivers()

import argparse
import hashlib
import importlib
import importlib.machinery
import json
import os
import random
import shutil
import stat
import subprocess
import sysconfig
import threading
import time
import traceback
from pathlib import Path

MAX_VIOLATIONS = 20


def parse_args(argv=None, *, tree_required=True):
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", required=tree_required, help="checkout of the measured head")
    parser.add_argument("--iters", type=int, default=int(os.environ.get("RACE_STRESS_ITERS", "20")))
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--mutant", action="store_true",
                        help="sensitivity check: break the target's guard (each driver says how); "
                             "the run must then report violations")
    args = parser.parse_args(argv)
    if args.seed is None:
        args.seed = int(os.environ.get("RACE_STRESS_SEED") or random.SystemRandom().randrange(2**32))
    return args


def runtime() -> dict:
    check = getattr(sys, "_is_gil_enabled", None)
    return {
        "python": sys.version.split()[0],
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "gil_enabled": True if check is None else bool(check()),
        "switch_interval": sys.getswitchinterval(),
    }


# Same overrides pins.git_env drops. Duplicated so a driver script does not import the test package.
_GIT_REPO_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_INDEX_FILE",
    "GIT_NAMESPACE", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES",
)


def _git_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if k not in _GIT_REPO_ENV and not k.startswith("GIT_CONFIG_")}


def _resolve_git() -> str:
    """Absolute git. Prefer ``/usr/bin/git`` over ``PATH``. Same rule as ``pins.GIT``.

    Residual: the bytes at that path are not pinned, and ``GIT_EXEC_PATH`` can still
    redirect git's subcommands.
    """
    trusted = "/usr/bin/git"
    if os.path.isfile(trusted) and os.access(trusted, os.X_OK):
        return trusted
    found = shutil.which("git")
    if found and os.path.isabs(found) and os.access(found, os.X_OK):
        return found
    return trusted


_GIT = _resolve_git()
_EXTENSION_SUFFIXES = tuple(importlib.machinery.EXTENSION_SUFFIXES)


def tree_info(tree: str, relpath: str) -> dict:
    """Pin what was measured: the tree's git HEAD and the target file's SHA-256."""
    path = Path(tree) / relpath
    try:
        head = subprocess.run([_GIT, "-C", tree, "rev-parse", "HEAD"], check=False, capture_output=True, text=True,
                              timeout=15, env=_git_env()).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        head = None
    return {"tree_head": head, "file": relpath, "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def prepare_tree_import(tree: str) -> None:
    """Put ``tree/core`` first on ``sys.path`` and do not write bytecode for it."""
    sys.dont_write_bytecode = True
    importlib.invalidate_caches()
    core = str(Path(tree) / "core")
    sys.path[:] = [entry for entry in sys.path if entry != core]
    sys.path.insert(0, core)
    importlib.invalidate_caches()


def _lexical(path: str) -> str:
    """Absolute path with ``.`` and ``..`` removed. Symlinks stay as written."""
    return os.path.normpath(os.path.abspath(path))


def _relative_to(path: str, parent: str) -> str | None:
    try:
        rel = Path(path).relative_to(parent)
    except ValueError:
        return None
    if ".." in rel.parts:
        return None
    text = rel.as_posix()
    return "" if text == "." else text


def _chain_symlink(root: str, rel: str) -> str | None:
    """First symlink in ``rel`` walked from ``root`` without following, or None.

    Scope stays on the path the importer recorded. ``Path.resolve`` is not used: it
    would follow ``core/dataclasses`` out of the tree and hide the shadow.
    """
    parts = [part for part in Path(rel).parts if part not in ("", ".")]
    if not parts:
        return None
    try:
        dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return rel
    try:
        acc: list[str] = []
        for index, part in enumerate(parts):
            acc.append(part)
            shown = "/".join(acc)
            try:
                st = os.stat(part, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                return shown
            if stat.S_ISLNK(st.st_mode):
                return shown
            if index == len(parts) - 1:
                return None
            if not stat.S_ISDIR(st.st_mode):
                return shown
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dirfd)
            except OSError:
                return shown
            os.close(dirfd)
            dirfd = nxt
        return None
    finally:
        try:
            os.close(dirfd)
        except OSError:
            pass


def _same_file(a: str, b: str) -> bool:
    try:
        left, right = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    if stat.S_ISLNK(left.st_mode) or stat.S_ISLNK(right.st_mode):
        return False
    if not stat.S_ISREG(left.st_mode) or not stat.S_ISREG(right.st_mode):
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _fs_locations(module) -> list[str]:
    """``__file__``, spec origin, and package search paths that name a filesystem location."""
    locs: list[str] = []
    file = getattr(module, "__file__", None)
    if isinstance(file, str):
        locs.append(file)
    spec = getattr(module, "__spec__", None)
    origin = getattr(spec, "origin", None) if spec is not None else None
    if isinstance(origin, str):
        locs.append(origin)
    path = getattr(module, "__path__", None)
    if path:
        locs.extend(str(item) for item in path)
    sub = getattr(spec, "submodule_search_locations", None) if spec is not None else None
    if sub:
        locs.extend(str(item) for item in sub)
    loader = getattr(spec, "loader", None) if spec is not None else None
    get = getattr(loader, "get_filename", None)
    if callable(get):
        try:
            filename = get()
        except (AttributeError, ImportError, OSError, TypeError, ValueError):
            filename = None
        if isinstance(filename, str):
            locs.append(filename)
    out: list[str] = []
    for loc in locs:
        if not loc or loc in ("built-in", "frozen") or loc.startswith("<"):
            continue
        if loc not in out:
            out.append(loc)
    return out


def _shadow_candidates(core: Path, top: str) -> list[str]:
    """Files under ``core/`` that FileFinder could load as the top-level name ``top``."""
    found: list[str] = []
    base = core / top
    try:
        base_link = base.is_symlink()
        base_dir = base.is_dir()
    except OSError:
        base_link = False
        base_dir = False
    if base_link or base_dir:
        for suffix in _IMPORT_SUFFIXES:
            init = base / f"__init__{suffix}"
            try:
                if init.is_symlink() or init.is_file():
                    found.append(str(init))
            except OSError:
                continue
        found.append(str(base))
    for suffix in _IMPORT_SUFFIXES:
        file = core / f"{top}{suffix}"
        try:
            if file.is_symlink() or file.is_file():
                found.append(str(file))
        except OSError:
            continue
    return found


def _inode_matches(path: str, candidates: list[str]) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    for candidate in candidates:
        try:
            other = os.stat(candidate)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == (other.st_dev, other.st_ino):
            return True
    return False


_IMPORT_SUFFIXES = tuple(
    importlib.machinery.EXTENSION_SUFFIXES
    + importlib.machinery.SOURCE_SUFFIXES
    + importlib.machinery.BYTECODE_SUFFIXES)


def _require_source(module, tree_lex: str, relpath: str) -> None:
    wanted = _lexical(str(Path(tree_lex) / relpath))
    name = getattr(module, "__name__", module)
    file_origin = getattr(module, "__file__", None)
    spec = getattr(module, "__spec__", None)
    spec_origin = getattr(spec, "origin", None) if spec is not None else None
    if not isinstance(file_origin, str) or not isinstance(spec_origin, str):
        raise SystemExit(f"{name} loaded from {file_origin}, not pinned source {wanted}")
    for origin in (file_origin, spec_origin):
        lex = _lexical(origin)
        rel = _relative_to(lex, tree_lex)
        link = None if rel is None else _chain_symlink(tree_lex, rel)
        if lex != wanted or rel is None or link is not None or not _same_file(lex, wanted):
            raise SystemExit(f"{name} loaded from {origin}, not pinned source {wanted}")


def _refuse_loaded_shadow(name, module, tree_lex: str, core: Path, core_lex: str, stdlib) -> None:
    top = name.split(".", 1)[0]
    candidates: list[str] | None = None
    for loc in _fs_locations(module):
        lex = _lexical(loc)
        rel_core = _relative_to(lex, core_lex)
        rel_tree = _relative_to(lex, tree_lex)
        if rel_core is not None:
            parts = Path(rel_core).parts
            if not parts or parts[0] != "recursion_core":
                raise SystemExit(
                    f"{name} loaded from {loc}; that top-level name on the inserted core/ path shadows the import")
            if rel_tree is not None and _chain_symlink(tree_lex, rel_tree) is not None:
                raise SystemExit(
                    f"{name} loaded from {loc}; path is not a real file in the pinned tree")
            if any(Path(lex).name.endswith(suffix) for suffix in _EXTENSION_SUFFIXES):
                raise SystemExit(f"{name} loaded extension {loc}, not the pinned source")
        elif top in stdlib:
            if candidates is None:
                candidates = _shadow_candidates(core, top)
            if candidates and _inode_matches(lex, candidates):
                raise SystemExit(
                    f"{name} loaded from {loc}; that top-level name on the inserted core/ path shadows the import")
    cached = getattr(module, "__cached__", None)
    file = getattr(module, "__file__", None)
    if isinstance(cached, str) and isinstance(file, str):
        rel_core = _relative_to(_lexical(file), core_lex)
        if (rel_core is not None and Path(rel_core).parts[:1] == ("recursion_core",)
                and Path(cached).is_file()):
            raise SystemExit(f"{name} loaded with bytecode cache {cached}")
    if top not in stdlib:
        return
    drivers = _lexical(str(Path(__file__).resolve().parent))
    for loc in _fs_locations(module):
        if any(_relative_to(_lexical(loc), root) is not None for root in _stdlib_roots()):
            continue
        if _relative_to(_lexical(loc), drivers) is not None:
            raise SystemExit(f"{name} loaded from {loc}; stdlib module loaded from drivers/")
        raise SystemExit(f"{name} loaded from {loc}, not the stdlib prefix")


def _stdlib_roots() -> tuple[str, ...]:
    """Directories from ``sysconfig`` that hold the standard library (under ``sys.base_prefix``)."""
    roots: list[str] = []
    for key in ("stdlib", "platstdlib"):
        raw = sysconfig.get_path(key)
        if isinstance(raw, str) and raw:
            lex = _lexical(raw)
            if lex not in roots:
                roots.append(lex)
    return tuple(roots)


def verify_loaded(tree: str, expected: list[tuple[object, str]]) -> None:
    """Exit when an imported module is not the pinned source.

    ``expected`` is ``(module, path relative to tree)``. Origins are compared as written
    and by ``lstat`` from the tree root; symlinks are not resolved to decide whether a
    file sits under ``core/``. The loader's filename is checked even when ``__file__``
    and ``spec.origin`` have been rewritten. A package directory or extension that took
    the place of the pinned source, a bytecode cache beside a loaded file, a module whose
    path leaves the tree through a symlink, or a standard-library name loaded from
    ``core/`` or from ``drivers/`` (anywhere outside the stdlib prefix) is refused.
    """
    tree_lex = _lexical(tree)
    core = Path(tree_lex) / "core"
    core_lex = _lexical(str(core))
    for module, relpath in expected:
        _require_source(module, tree_lex, relpath)
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    for name, module in list(sys.modules.items()):
        _refuse_loaded_shadow(name, module, tree_lex, core, core_lex, stdlib)


class Violations:
    """Thread-safe violation sink. Keeps the first few messages and a total count."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0
        self.first: list[str] = []

    def add(self, message: str) -> None:
        with self._lock:
            self.count += 1
            if len(self.first) < MAX_VIOLATIONS:
                self.first.append(message)

    def check(self, ok: bool, message: str) -> None:
        if not ok:
            self.add(message)


class NoLock:
    """Mutant stand-in for a target's lock: never excludes anyone."""

    def __enter__(self):
        time.sleep(0)
        return self

    def __exit__(self, *exc):
        return False

    def acquire(self, *args, **kwargs):
        return True

    def release(self):
        pass


class StampLock:
    """Drop-in for a target's ``threading.Lock`` that stamps each acquisition.

    Every operation of the targets measured here takes its object's one lock exactly once, so
    the stamp taken under that lock is the operation's linearization point. Sorting a
    concurrent history by stamp and replaying it on a fresh object must reproduce every result.
    """

    def __init__(self, *, hold: bool = True) -> None:
        self._lock = threading.Lock()
        self._next = 0
        self._hold = hold  # False: the sensitivity mutant, which leaves the critical section unguarded
        self.local = threading.local()

    def __enter__(self):
        self._lock.acquire()
        self._next += 1  # only ever written while holding _lock
        self.local.stamp = self._next
        if not self._hold:
            self._lock.release()
            time.sleep(0)
        return self

    def __exit__(self, *exc):
        if self._hold:
            self._lock.release()
        return False

    def acquire(self, *args, **kwargs):
        got = self._lock.acquire(*args, **kwargs)
        if got:
            self._next += 1
            self.local.stamp = self._next
        return got

    def release(self):
        self._lock.release()

    def take(self) -> int | None:
        stamp = getattr(self.local, "stamp", None)
        self.local.stamp = None
        return stamp


def hammer(workers, *, timeout: float = 60.0, threads_out: list | None = None) -> list[str]:
    """Run each callable on its own thread, released together by a barrier.

    Returns a list of problems: uncaught exceptions (with traceback tails) and threads that did
    not finish within ``timeout`` (a hang is reported, never waited out). When ``threads_out``
    is given, the started threads are appended to it so the caller can join them later.
    """
    barrier = threading.Barrier(len(workers))
    problems: list[str] = []
    lock = threading.Lock()

    def wrap(fn):
        def run():
            try:
                barrier.wait()
                fn()
            except BaseException:  # noqa: BLE001 - every escape is a finding
                with lock:
                    problems.append("".join(traceback.format_exc().splitlines(True)[-4:]))
        return run

    threads = [threading.Thread(target=wrap(fn), daemon=True) for fn in workers]
    if threads_out is not None:
        threads_out.extend(threads)
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    hung = sum(thread.is_alive() for thread in threads)
    if hung:
        problems.append(f"{hung} thread(s) still running after {timeout}s (deadlock or livelock)")
    return problems


def emit(target: str, args, violations: Violations, *, started: float, extra: dict | None = None) -> None:
    out = {
        "target": target,
        "iters": args.iters,
        "threads": args.threads,
        "seed": args.seed,
        "mutant": args.mutant,
        "runtime": runtime(),
        "violation_count": violations.count,
        "violations": violations.first,
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    out.update(extra or {})
    print(json.dumps(out, sort_keys=True))


def finish(target: str, args, violations: Violations, *, started: float, extra: dict | None = None) -> int:
    """Print the result line and return 1 when any violation was recorded."""
    emit(target, args, violations, started=started, extra=extra)
    return 1 if violations.count else 0


_pins_mod = None


def _pins():
    """The checkout's ``pins.py``, loaded by path so the measured tree cannot shadow it."""
    global _pins_mod
    if _pins_mod is None:
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "pins.py"
        spec = importlib.util.spec_from_file_location("_race_stress_pins_runtime", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _pins_mod = module
    return _pins_mod


def arm_tree_pin(tree: str) -> None:
    """Record executed code from here on. Call after ``prepare_tree_import`` and before the import."""
    _pins().arm_execution_pin(tree)


def enforce_tree_pin(tree: str, violations: Violations) -> None:
    """Record a violation when the code this process executed is not the hashed pin."""
    pins = _pins()
    root = Path(tree)
    env = pins.env_for_root(root)
    if env is not None:
        ok, message = pins.pin_status(env, root)
        if not ok:
            violations.add(message)
        return
    for problem in pins.execution_problems():
        violations.add("unpinned: " + problem)
