"""Stress the plugin_security connection breaker (recursion #328, CSL-720) from many threads.

Measured code: ``recursion_core.plugin_security.breaker.ConnectionBreaker`` in ``--tree``.
Since #328 ``afe38c1f`` the breaker keeps bindings, faults and trips in a SQLite store the
installer creates (``create_breaker_store``), binds each run to a connection, plugin, provider
and model, and hands out one probe lease per tripped connection (``begin_probe``);
``reset_after_probe(connection, lease, ok)`` clears the trip only for that live lease, and a
new trip drops the lease. Each iteration uses a fresh store in a temporary directory and a
fixed fake clock (leases never expire on their own here).

Part 1 (linearizability). Concurrent ``bind_run``/``report``/``fault_run``/``begin_probe``/
``reset_after_probe``/``check`` with the breaker's lock swapped for a stamping lock. Replaying
the history in stamp order on a fresh breaker and store must reproduce every result (return
value or ``Refused`` code) and the final store and lease state. Lease ids are random, so the
replay maps each concurrent lease to the one the replay was handed at the same point.
``tripped()`` is called for crash-freedom only and left out of the replay.

Part 2 (stale probe, CSL-804). A prober leases a probe, finishes it, snapshots how many trips
have completed, yields (handing the result back), then reports success with its lease;
reporter threads keep tripping the connection. A *lost trip* is a successful reset made after
a trip that completed strictly after the probe ended: the newer limit erased by evidence older
than it. Reported as ``lost_trips``; at heads before the lease API this was the CSL-804 race.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

import _harness as h

CONNECTIONS = ("conn-a", "conn-b")
RUNS = ("run-1", "run-2", "run-3")
PLUGINS = ("plug-1", "plug-2")
EVENTS = ("plan_limit", "rate_limit", "usage_window_exhausted", "core_timeout", "untyped thing")


def _new_breaker(mod, tmp: str, name: str):
    path = os.path.join(tmp, name)
    mod.create_breaker_store(path)
    return mod.ConnectionBreaker(path, clock=lambda: 0.0)


def _store_state(breaker) -> tuple:
    db = breaker._db
    return (sorted(db.execute("SELECT * FROM runs").fetchall()),
            sorted(db.execute("SELECT * FROM faulted").fetchall()),
            sorted(db.execute("SELECT * FROM tripped").fetchall()),
            sorted(breaker._leases))


def linearizability(mod, Refused, rng, args, v: h.Violations, counters: dict, tmp: str) -> None:
    breaker = _new_breaker(mod, tmp, "concurrent.db")
    stamps = h.StampLock(hold=not args.mutant)
    breaker._lock = stamps
    history: list[tuple] = []
    hist_lock = threading.Lock()
    leases: list[tuple[str, str]] = []  # (connection, lease) handed out concurrently

    def call(name, *a):
        stamps.take()
        try:
            result = ("ok", getattr(breaker, name)(*a))
        except Refused as exc:
            result = ("refused", exc.code)
        except Exception as exc:  # noqa: BLE001 - e.g. sqlite misuse when the mutant drops the lock
            result = ("raised", type(exc).__name__)
            v.add(f"{name}{a} raised {exc!r}")
        stamp = stamps.take()
        if name == "tripped":
            v.check(result[0] == "ok" and type(result[1]) is bool, f"tripped returned {result!r}")
            return result
        if stamp is None:
            # report() with a non-trip or untyped event returns before the lock.
            v.check(name == "report" and (a[1] in mod._NOT_ENGINE_FAULTS or a[1] not in mod.TYPED_LIMITS),
                    f"{name}{a} returned without taking the breaker lock")
            return result
        with hist_lock:
            history.append((stamp, name, a, result))
        return result

    def worker(seed):
        wrng = random.Random(seed)
        for _ in range(80):
            conn, run, plug = wrng.choice(CONNECTIONS), wrng.choice(RUNS), wrng.choice(PLUGINS)
            roll = wrng.random()
            if roll < 0.12:
                call("bind_run", run, conn, plug, "prov", "model-1")
            elif roll < 0.30:
                call("report", conn, wrng.choice(EVENTS))
            elif roll < 0.36:
                call("fault_run", run)
            elif roll < 0.48:
                got = call("begin_probe", conn)
                if got[0] == "ok":
                    with hist_lock:
                        leases.append((conn, got[1]))
            elif roll < 0.62:
                with hist_lock:
                    pick = wrng.choice(leases) if leases and wrng.random() < 0.85 else (conn, "0" * 32)
                call("reset_after_probe", pick[0], pick[1], wrng.random() < 0.8)
            elif roll < 0.92:
                call("check", run, conn, plug)
            else:
                call("tripped", conn)

    for problem in h.hammer([lambda s=rng.randrange(2**32): worker(s) for _ in range(args.threads)]):
        v.add(problem)
    history.sort(key=lambda e: e[0])
    seq = _new_breaker(mod, tmp, "replay.db")
    lease_map: dict[str, str] = {}
    for stamp, name, a, result in history:
        if name == "reset_after_probe":
            a = (a[0], lease_map.get(a[1], a[1]), a[2])
        try:
            got = ("ok", getattr(seq, name)(*a))
        except Refused as exc:
            got = ("refused", exc.code)
        except Exception as exc:  # noqa: BLE001
            got = ("raised", type(exc).__name__)
        if name == "begin_probe" and result[0] == "ok" and got[0] == "ok":
            lease_map[result[1]] = got[1]
            got = result  # same outcome; ids differ by construction
        if got != result:
            v.add(f"#{stamp} {name}{a}: concurrent {result!r} != sequential {got!r}")
    concurrent_state = _store_state(breaker)
    replay_state = _store_state(seq)
    inverse = {r: c for c, r in lease_map.items()}
    replay_state = replay_state[:3] + (sorted(inverse.get(k, k) for k in replay_state[3]),)
    v.check(concurrent_state[:3] == replay_state[:3], "final store state differs from replay")
    v.check(len(concurrent_state[3]) == len(replay_state[3]), "final lease count differs from replay")
    counters["ops"] += len(history)
    counters["leases"] += sum(1 for e in history if e[1] == "begin_probe" and e[3][0] == "ok")
    counters["leased_resets"] += sum(1 for e in history if e[1] == "reset_after_probe" and e[3] == ("ok", True))
    breaker.close()
    seq.close()


def stale_probe(mod, Refused, rng, args, v: h.Violations, counters: dict, tmp: str) -> None:
    breaker = _new_breaker(mod, tmp, "stale.db")
    breaker.report("conn-a", "plan_limit")
    completed = [0]  # trips fully returned; only reporters write it, under count_lock
    count_lock = threading.Lock()
    stop = threading.Event()
    lost = [0]
    resets = [0]
    leased = [0]

    def reporter(seed):
        wrng = random.Random(seed)
        while not stop.is_set():
            breaker.report("conn-a", "plan_limit")
            with count_lock:
                completed[0] += 1
            if wrng.random() < 0.5:
                time.sleep(0)

    def prober():
        try:
            for _ in range(200):
                try:
                    lease = breaker.begin_probe("conn-a")
                except Refused:
                    time.sleep(0)
                    continue
                leased[0] += 1
                time.sleep(0)  # the probe itself runs here and finishes
                with count_lock:
                    probe_ended = completed[0]
                time.sleep(0)  # the caller hands the result back
                with count_lock:
                    before_reset = completed[0]
                if breaker.reset_after_probe("conn-a", lease, True):
                    resets[0] += 1
                    if before_reset > probe_ended:
                        lost[0] += 1  # a trip that completed after the probe ended was erased by it
                    breaker.report("conn-a", "plan_limit")  # trip again for the next probe
        finally:
            stop.set()

    reporters = [lambda s=rng.randrange(2**32): reporter(s) for _ in range(max(1, args.threads // 2))]
    for problem in h.hammer(reporters + [prober]):
        v.add(problem)
    counters["lost_trips"] += lost[0]
    counters["probe_resets"] += resets[0]
    counters["stale_probe_leases"] += leased[0]
    breaker.close()


def main() -> None:
    args = h.parse_args()
    sys.path.insert(0, str(Path(args.tree) / "core"))
    from recursion_core.plugin_security import breaker as mod
    from recursion_core.plugin_security.errors import Refused

    started = time.monotonic()
    v = h.Violations()
    rng = random.Random(args.seed)
    counters = {"ops": 0, "leases": 0, "leased_resets": 0, "lost_trips": 0, "probe_resets": 0,
                "stale_probe_leases": 0}
    for _ in range(args.iters):
        with tempfile.TemporaryDirectory(prefix="rs328-") as tmp:
            linearizability(mod, Refused, rng, args, v, counters, tmp)
            stale_probe(mod, Refused, rng, args, v, counters, tmp)
    h.emit("breaker_328", args, v, started=started,
           extra={"measured": h.tree_info(args.tree, "core/recursion_core/plugin_security/breaker.py"),
                  "counters": counters})


if __name__ == "__main__":
    main()
