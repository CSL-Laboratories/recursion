"""Stress the CSL-738 daemon registry (recursion #324) from many threads.

Measured code: ``recursion_core.daemon`` in the tree passed with ``--tree``. The service
manager is an in-memory fake; no systemd, no unit is started on the host.

Invariants checked every iteration:

* intent before effect: a unit is started only after its run row is committed;
* one launch per run id: concurrent ``start`` of one id launches its unit at most once,
  and every losing call fails with ``sqlite3.IntegrityError``;
* B14: a run whose Stop is recorded is never read as ``running`` (snapshots taken while
  writers run, and the final state), even when a stale caller writes ``running``;
* terminal states are sticky: once a run reads ``terminated`` or ``lost_reboot`` it never
  reads anything else;
* the sweep never stops a unit outside the reserved ``recursion-run-*`` namespace;
* ``close`` racing in-flight calls yields only ``RuntimeError('... closed')``, never a
  sqlite error from a closed connection, and the file reopens with every committed row;
* ``UserLock``: of N threads racing for the lock, exactly one holds it.
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import _harness as h

RESERVED_PREFIX = "recursion-run-"
FOREIGN = "user-app.service"


class FakeManager:
    """Thread-safe in-memory stand-in for the systemd user manager, with injected faults."""

    def __init__(self, registry, violations, rng_seed):
        self.registry, self.v = registry, violations
        self.lock = threading.Lock()
        self.units = {FOREIGN: "running"}
        self.starts: dict[str, int] = {}
        self.stopped: list[str] = []
        self.local = threading.local()
        self.seed = rng_seed

    def _rng(self):
        rng = getattr(self.local, "rng", None)
        if rng is None:
            rng = self.local.rng = random.Random(f"{self.seed}:{threading.get_ident()}")
        return rng

    def start(self, unit, argv):
        run_id = unit[len(RESERVED_PREFIX):-len(".service")]
        try:
            row = self.registry.get(run_id)  # intent must already be durable
            self.v.check(row.unit == unit, f"start: row unit {row.unit} != {unit}")
        except KeyError:
            self.v.add(f"start: unit {unit} launched with no committed run row")
        except RuntimeError:
            pass  # registry closed under us; the daemon lock makes this impossible, see final checks
        with self.lock:
            self.starts[unit] = self.starts.get(unit, 0) + 1
            if self._rng().random() < 0.05:
                raise OSError("injected start failure")
            self.units[unit] = "running"

    def stop(self, unit):
        with self.lock:
            self.stopped.append(unit)
            if self._rng().random() < 0.05:
                raise OSError("injected stop failure")
            if unit in self.units:
                self.units[unit] = "empty"

    def observe(self, unit):
        with self.lock:
            if self._rng().random() < 0.05:
                raise OSError("injected observe failure")
            return self.units.get(unit, "unknown")

    def owned_units(self):
        with self.lock:
            return {u for u in self.units if u.startswith(RESERVED_PREFIX) or u == FOREIGN}

    def add_orphan(self, name):
        with self.lock:
            self.units[f"{RESERVED_PREFIX}{name}.service"] = "running"


ALLOWED = (KeyError, sqlite3.IntegrityError, OSError)


def one_iteration(mod, rng, args, v: h.Violations, counters: dict) -> None:
    tmp = tempfile.mkdtemp(prefix="race324-")
    reg_dir = Path(tmp) / "reg"
    registry = mod.Registry(reg_dir)
    manager = FakeManager(registry, v, rng.randrange(2**32))
    boot = {"id": "boot-A"}
    daemon = mod.Daemon(registry, manager, boot_id=lambda: boot["id"])
    if args.mutant:  # sensitivity: drop both locks, so calls share one sqlite connection unguarded
        registry.lock = daemon.lock = h.NoLock()
    ids = [f"r{i}" for i in range(6)]
    done = threading.Event()
    close_this_iter = rng.random() < 0.3
    flip_boot = rng.random() < 0.3
    closed = threading.Event()
    wins: dict[str, int] = {}
    wins_lock = threading.Lock()

    def classify(exc: BaseException, where: str) -> None:
        if isinstance(exc, RuntimeError) and "closed" in str(exc):
            v.check(closed.is_set(), f"{where}: 'closed' raised before close was called")
            return
        if isinstance(exc, ALLOWED):
            return
        v.add(f"{where}: unexpected {type(exc).__name__}: {exc}")

    def worker(seed):
        wrng = random.Random(seed)
        for _ in range(40):
            op = wrng.random()
            rid = wrng.choice(ids)
            try:
                if op < 0.3:
                    daemon.start(rid, ["true"])
                    with wins_lock:
                        wins[rid] = wins.get(rid, 0) + 1
                elif op < 0.55:
                    daemon.stop(rid)
                elif op < 0.7:
                    daemon.sweep()
                elif op < 0.9:
                    # A stale caller (B14): read, yield, then write running from the old read.
                    run = registry.get(rid)
                    time.sleep(0)
                    registry.state(run.run_id, "running", "stale writer")
                else:
                    manager.add_orphan(f"orphan{wrng.randrange(3)}")
            except BaseException as exc:  # noqa: BLE001
                classify(exc, f"op<{op:.2f}> {rid}")

    terminal_seen: dict[str, str] = {}

    def observer():
        while not done.is_set():
            try:
                runs = registry.all()
            except RuntimeError as exc:
                classify(exc, "observer")
                return
            except BaseException as exc:  # noqa: BLE001
                classify(exc, "observer")
                continue
            for run in runs:
                v.check(not (run.stop_requested and run.state == "running"),
                        f"B14: {run.run_id} stop recorded but read as running")
                if run.run_id in terminal_seen:
                    v.check(run.state == terminal_seen[run.run_id],
                            f"terminal {run.run_id} left {terminal_seen[run.run_id]} for {run.state}")
                elif run.state in mod.TERMINAL:
                    terminal_seen[run.run_id] = run.state

    def closer():
        time.sleep(rng.random() * 0.01)
        closed.set()
        daemon.close()

    def flipper():
        time.sleep(rng.random() * 0.01)
        boot["id"] = "boot-B"

    workers = [lambda s=rng.randrange(2**32): worker(s) for _ in range(args.threads)]
    if close_this_iter:
        workers.append(closer)
    if flip_boot:
        workers.append(flipper)
    watcher = threading.Thread(target=observer, daemon=True)
    watcher.start()
    for problem in h.hammer(workers, timeout=120):
        v.add(problem)
    done.set()
    watcher.join(30)
    v.check(not watcher.is_alive(), "observer did not finish")

    if not closed.is_set():
        daemon.close()
    for unit, count in manager.starts.items():
        v.check(count <= 1, f"unit {unit} launched {count} times")
    for rid, count in wins.items():
        v.check(count <= 1, f"start({rid}) succeeded {count} times")
    v.check(FOREIGN not in manager.stopped, "sweep stopped a unit outside the reserved namespace")

    reopened = mod.Registry(reg_dir)
    try:
        final = {r.run_id: r for r in reopened.all()}
        for unit in manager.starts:
            rid = unit[len(RESERVED_PREFIX):-len(".service")]
            v.check(rid in final, f"unit {unit} was launched but its row is not in the reopened file")
        for run in final.values():
            v.check(not (run.stop_requested and run.state == "running"), f"B14 final: {run.run_id}")
            if run.run_id in terminal_seen:
                v.check(run.state == terminal_seen[run.run_id], f"terminal final: {run.run_id}")
        counters["runs"] += len(final)
        counters["closed_mid_run"] += int(close_this_iter)
        counters["boot_flips"] += int(flip_boot)
        counters["terminal"] += sum(r.state in mod.TERMINAL for r in final.values())
        counters["stop_requested"] += sum(r.stop_requested for r in final.values())
    finally:
        reopened.close()


def lock_iteration(mod, args, v: h.Violations) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="race324lock-"))
    os.chmod(runtime, 0o700)
    held, refused = [], []
    hold = threading.Barrier(args.threads, timeout=30)
    guard = threading.Lock()

    def contender():
        lock = mod.UserLock(runtime)
        try:
            lock.__enter__()
        except mod.AlreadyRunning:
            with guard:
                refused.append(1)
            hold.wait()
            return
        with guard:
            held.append(lock)
        hold.wait()  # everyone has tried while the winner still holds it
        lock.__exit__(None, None, None)

    for problem in h.hammer([contender] * args.threads):
        v.add(problem)
    v.check(len(held) == 1 and len(refused) == args.threads - 1,
            f"UserLock: {len(held)} holders, {len(refused)} refused of {args.threads}")


def main() -> None:
    args = h.parse_args()
    sys.path.insert(0, str(Path(args.tree) / "core"))
    from recursion_core import daemon as mod

    logging.disable(logging.CRITICAL)  # the injected manager faults are logged by design
    started = time.monotonic()
    v = h.Violations()
    rng = random.Random(args.seed)
    counters = {"runs": 0, "closed_mid_run": 0, "boot_flips": 0, "terminal": 0, "stop_requested": 0}
    for _ in range(args.iters):
        one_iteration(mod, rng, args, v, counters)
        lock_iteration(mod, args, v)
    h.emit("daemon_324", args, v, started=started,
           extra={"measured": h.tree_info(args.tree, "core/recursion_core/daemon/__init__.py"),
                  "counters": counters})


if __name__ == "__main__":
    main()
