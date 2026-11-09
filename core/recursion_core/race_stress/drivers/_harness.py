"""Shared stress harness for the race_stress drivers (CSL-801). Standard library only.

Each driver runs as its own script so it can put a measured tree's ``core/`` first on
``sys.path`` without clashing with this checkout's ``recursion_core``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
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


def tree_info(tree: str, relpath: str) -> dict:
    """Pin what was measured: the tree's git HEAD and the target file's SHA-256."""
    path = Path(tree) / relpath
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    try:
        head = subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], check=False, capture_output=True, text=True,
                              timeout=15, env=env).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        head = None
    return {"tree_head": head, "file": relpath, "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


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


def hammer(workers, *, timeout: float = 60.0) -> list[str]:
    """Run each callable on its own thread, released together by a barrier.

    Returns a list of problems: uncaught exceptions (with traceback tails) and threads that did
    not finish within ``timeout`` (a hang is reported, never waited out).
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
