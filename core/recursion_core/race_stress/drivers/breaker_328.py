"""Stress the plugin_security connection breaker (recursion #328, CSL-720) from many threads.

Measured code: ``recursion_core.plugin_security.breaker.ConnectionBreaker`` in ``--tree``.

Part 1 (linearizability). Concurrent ``bind_run``/``report``/``fault_run``/
``reset_after_probe``/``check`` with the breaker's lock swapped for a stamping lock. Replaying
the history in stamp order on a fresh breaker must reproduce every result (return value or
``Refused`` code) and the final state. ``tripped()`` reads without the lock, so it is called
for crash-freedom only and left out of the replay.

Part 2 (stale probe). The module says a tripped connection "resets only through a successful
leased probe", but ``reset_after_probe(connection, True)`` takes no probe identity, lease or
trip generation. A prober thread finishes a probe, snapshots how many trips have completed, yields
(handing the result back), then reports success; reporter threads keep tripping the
connection. A *lost trip* is a successful reset made after a trip that completed strictly
after the probe ended: the newer limit is erased by evidence older than it. The count is reported as ``lost_trips``; any
non-zero count is the race this lane files.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path

import _harness as h

CONNECTIONS = ("conn-a", "conn-b")
RUNS = ("run-1", "run-2", "run-3")
EVENTS = ("plan_limit", "rate_limit", "usage_window_exhausted", "core_timeout", "untyped thing")


def linearizability(mod, Refused, rng, args, v: h.Violations, counters: dict) -> None:
    breaker = mod.ConnectionBreaker()
    stamps = h.StampLock(hold=not args.mutant)
    breaker._lock = stamps
    history: list[tuple] = []
    hist_lock = threading.Lock()

    def call(name, *a):
        stamps.take()
        try:
            result = ("ok", getattr(breaker, name)(*a))
        except Refused as exc:
            result = ("refused", exc.code)
        stamp = stamps.take()
        if name == "tripped":
            v.check(type(result[1]) is bool, f"tripped returned {result!r}")
            return
        if stamp is None:
            # report() with a non-trip event or an untyped event returns before the lock.
            v.check(name == "report" and (a[1] in mod._NOT_ENGINE_FAULTS or a[1] not in mod.TYPED_LIMITS),
                    f"{name}{a} returned without taking the breaker lock")
            return
        with hist_lock:
            history.append((stamp, name, a, result))

    def worker(seed):
        wrng = random.Random(seed)
        for _ in range(80):
            conn, run = wrng.choice(CONNECTIONS), wrng.choice(RUNS)
            roll = wrng.random()
            if roll < 0.15:
                call("bind_run", run, conn)
            elif roll < 0.35:
                call("report", conn, wrng.choice(EVENTS))
            elif roll < 0.42:
                call("fault_run", run)
            elif roll < 0.6:
                call("reset_after_probe", conn, wrng.random() < 0.8)
            elif roll < 0.9:
                call("check", run, conn)
            else:
                call("tripped", conn)

    for problem in h.hammer([lambda s=rng.randrange(2**32): worker(s) for _ in range(args.threads)]):
        v.add(problem)
    history.sort(key=lambda e: e[0])
    seq = mod.ConnectionBreaker()
    for stamp, name, a, result in history:
        try:
            got = ("ok", getattr(seq, name)(*a))
        except Refused as exc:
            got = ("refused", exc.code)
        if got != result:
            v.add(f"#{stamp} {name}{a}: concurrent {result!r} != sequential {got!r}")
    for attr in ("_tripped", "_faulted_runs", "_run_connection"):
        v.check(getattr(seq, attr) == getattr(breaker, attr), f"final {attr} differs from replay")
    counters["ops"] += len(history)


def stale_probe(mod, rng, args, v: h.Violations, counters: dict) -> None:
    breaker = mod.ConnectionBreaker()
    completed = [0]  # trips fully returned; only reporters write it, under count_lock
    count_lock = threading.Lock()
    stop = threading.Event()
    lost = [0]
    resets = [0]

    def reporter(seed):
        wrng = random.Random(seed)
        while not stop.is_set():
            breaker.report("conn-a", "plan_limit")
            with count_lock:
                completed[0] += 1
            if wrng.random() < 0.5:
                time.sleep(0)

    def prober():
        for _ in range(200):
            time.sleep(0)  # the probe itself runs here and finishes
            with count_lock:
                probe_ended = completed[0]
            time.sleep(0)  # the caller hands the result back
            with count_lock:
                before_reset = completed[0]
            if breaker.reset_after_probe("conn-a", True):
                resets[0] += 1
                if before_reset > probe_ended:
                    lost[0] += 1  # a trip that completed after the probe ended was erased by it
        stop.set()

    reporters = [lambda s=rng.randrange(2**32): reporter(s) for _ in range(max(1, args.threads // 2))]
    for problem in h.hammer(reporters + [prober]):
        v.add(problem)
    counters["lost_trips"] += lost[0]
    counters["probe_resets"] += resets[0]


def main() -> None:
    args = h.parse_args()
    sys.path.insert(0, str(Path(args.tree) / "core"))
    from recursion_core.plugin_security import breaker as mod
    from recursion_core.plugin_security.errors import Refused

    started = time.monotonic()
    v = h.Violations()
    rng = random.Random(args.seed)
    counters = {"ops": 0, "lost_trips": 0, "probe_resets": 0}
    for _ in range(args.iters):
        linearizability(mod, Refused, rng, args, v, counters)
        stale_probe(mod, rng, args, v, counters)
    h.emit("breaker_328", args, v, started=started,
           extra={"measured": h.tree_info(args.tree, "core/recursion_core/plugin_security/breaker.py"),
                  "counters": counters})


if __name__ == "__main__":
    main()
