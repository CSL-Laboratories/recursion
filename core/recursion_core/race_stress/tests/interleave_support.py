"""Shared helpers for the adversarial interleaving tests (CSL-801, L11b).

The code under test lives on stacked heads that are not on master yet, and every one of those
heads ships its own ``recursion_core`` package. ``load_stacked`` imports one module from one
head without running that head's package ``__init__`` and without leaving its
``recursion_core`` in ``sys.modules``, so several heads can be loaded side by side in one
process. Each head is located by an environment variable that names a checkout of it; a test
whose head is not set is skipped, never faked.

    RACE_STRESS_TREE_324           recursion #324 checkout (daemon registry, CSL-738)
    RACE_STRESS_TREE_326           recursion #326 checkout (routes ConnectionBreaker, CSL-797)
    RACE_STRESS_TREE_328           recursion #328 checkout (plugin_security ConnectionBreaker)

Paper and mock only: nothing here starts a process, opens a socket or reads a credential.
"""

from __future__ import annotations

import importlib
import os
import random
import sys
import threading
import time
import types
import unittest
from pathlib import Path

from recursion_core.race_stress.pins import require_pinned

ITERATIONS = int(os.environ.get("RACE_STRESS_ITERS", "20"))
THREADS = int(os.environ.get("RACE_STRESS_THREADS", "8"))
# "0" is a real seed. Only a missing or blank value is random, and the draw happens at import.
_seed_raw = os.environ.get("RACE_STRESS_SEED")
SEED = (random.SystemRandom().randrange(1, 2**31)
        if _seed_raw is None or _seed_raw.strip() == "" else int(_seed_raw))

_load_lock = threading.Lock()
_cache: dict[tuple[str, str], types.ModuleType] = {}


def free_threaded() -> bool:
    check = getattr(sys, "_is_gil_enabled", None)
    return check is not None and not check()


def ref_root(env: str) -> Path:
    value = os.environ.get(env)
    if not value:
        raise unittest.SkipTest(f"{env} not set; point it at a checkout of the stacked head")
    root = Path(value)
    if not root.is_dir():
        raise unittest.SkipTest(f"{env}={value} is not a directory")
    require_pinned(env, root)  # skips, with a message, on a tree nobody pinned
    return root


def origin_error(module, root: Path, parts: list[str]) -> str | None:
    """Why ``module`` is not the source file under ``root``, or None when it is.

    If ``foo.py`` exists, a ``foo/`` package is a shadow and is refused. A bytecode
    cache next to the loaded file is refused: an unchecked-hash ``.pyc`` is executed
    under ``-B`` while ``__file__`` still names the ``.py``.
    """
    py = root.joinpath(*parts).with_suffix(".py")
    init = root.joinpath(*parts, "__init__.py")
    expected = py if py.is_file() else init
    origin = getattr(module, "__file__", None)
    name = getattr(module, "__name__", module)
    if origin is None or Path(origin).resolve() != expected.resolve():
        return f"{name} loaded from {origin}, not pinned source {expected}"
    cached = getattr(module, "__cached__", None)
    if cached and Path(cached).is_file():
        return f"{name} loaded with bytecode cache {cached}"
    return None


def load_stacked(env: str, dotted: str, src: str = "core") -> types.ModuleType:
    """Import ``dotted`` from the head named by ``env`` with its packages' ``__init__`` skipped."""
    root = ref_root(env) / src
    top = dotted.split(".", 1)[0]
    with _load_lock:
        if (env, dotted) in _cache:
            return _cache[(env, dotted)]
        saved = {k: v for k, v in sys.modules.items() if k == top or k.startswith(top + ".")}
        for k in saved:
            del sys.modules[k]
        try:
            parts = dotted.split(".")
            for i in range(1, len(parts)):
                name = ".".join(parts[:i])
                pkg = types.ModuleType(name)
                pkg.__path__ = [str(root.joinpath(*parts[:i]))]
                sys.modules[name] = pkg
            if not (root.joinpath(*parts).with_suffix(".py").is_file() or root.joinpath(*parts).is_dir()):
                raise unittest.SkipTest(f"{dotted} not found under {root}")
            sys.dont_write_bytecode = True
            importlib.invalidate_caches()
            module = importlib.import_module(dotted)
            problem = origin_error(module, root, parts)
            if problem:
                raise RuntimeError(problem)
        finally:
            for k in [k for k in sys.modules if k == top or k.startswith(top + ".")]:
                del sys.modules[k]
            sys.modules.update(saved)
        _cache[(env, dotted)] = module
        return module


def run_threads(targets, *, timeout: float = 60.0) -> list[BaseException]:
    """Start every callable behind one barrier; return the exceptions they raised."""
    barrier = threading.Barrier(len(targets))
    errors: list[BaseException] = []
    lock = threading.Lock()

    def wrap(fn):
        def body():
            try:
                barrier.wait()
                fn()
            except BaseException as exc:  # noqa: BLE001 - collected and reported by the caller
                with lock:
                    errors.append(exc)
        return body

    threads = [threading.Thread(target=wrap(fn), daemon=True) for fn in targets]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    hung = [t for t in threads if t.is_alive()]
    if hung:
        errors.append(TimeoutError(f"{len(hung)} thread(s) still running after {timeout}s"))
    return errors


class Gate:
    """A pause point: the thread that reaches ``hit`` blocks until the test calls ``release``."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self._go = threading.Event()

    def hit(self, timeout: float = 30.0) -> None:
        self.reached.set()
        if not self._go.wait(timeout):
            raise TimeoutError("gate never released")

    def wait_reached(self, timeout: float = 30.0) -> None:
        if not self.reached.wait(timeout):
            raise TimeoutError("gate never reached")

    def release(self) -> None:
        self._go.set()


class SeededCase(unittest.TestCase):
    """Prints the seed on failure so a randomized interleaving can be replayed with RACE_STRESS_SEED."""

    def rng(self, salt: str) -> random.Random:
        return random.Random(f"{SEED}:{type(self).__name__}:{salt}")

    def fail_with_seed(self, msg: str) -> None:
        self.fail(f"{msg} [RACE_STRESS_SEED={SEED}]")
