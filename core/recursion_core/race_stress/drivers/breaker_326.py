"""Stress the connection-keyed route breaker (recursion #326, CSL-797) from many threads.

Measured code: ``recursion_core.routes.breaker.ConnectionBreaker`` in ``--tree``. Time is a
fake clock the workers advance; nothing is launched and no vendor is called.

Each iteration runs concurrent ``admit``/``record``/``reset``/``state`` on a few connections,
models and tenants, with probes reported by their holder, by a stale holder or never. The
breaker's lock is swapped for a stamping lock, so every call gets its linearization point.
Checks:

* linearizability: replaying the history in stamp order on a fresh breaker (each call given
  the clock values it read) reproduces every result and the final state;
* one probe per key: in that order, no key leases a second half-open probe while an earlier
  probe's lease is live and unresolved (closed, released, reopened or reset). The lease is
  ``probe_lease_s`` when ``admit`` is given no deadline, and ``deadline_s + probe_grace_s``
  when it is (the length ``RouteService`` uses). A probe whose holder reports a limit frees
  its lease on the key it held even when the limit opens a different key (#326 ``_open``
  releases the probe first);
* no call raises. The process exits 1 when any violation was recorded.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path

import _harness as h

CONNECTIONS = ("sub:alice", "api:k1")
MODELS = ("m-large", "m-small")
TENANTS = ("t1", "t2")
PARAMS = dict(cooldown_s=1.0, max_cooldown_s=4.0, max_open_s=10.0, probe_lease_s=2.0,
              short_fault_s=0.5, probe_grace_s=30.0)


def _lease_seconds(deadline_s) -> float:
    """How long an admit keeps the key. ``None`` is the no-deadline branch."""
    if deadline_s is None:
        return PARAMS["probe_lease_s"]
    return float(deadline_s) + PARAMS["probe_grace_s"]


class FakeClock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = 1000.0
        self.local = threading.local()

    def advance(self, dt: float) -> None:
        with self._lock:
            self._now += dt

    def __call__(self) -> float:
        with self._lock:
            now = self._now
        self.local.reads.append(now)
        return now


def outcome(rng: random.Random, c, connection: str, model: str, now: float) -> dict:
    roll = rng.random()
    if roll < 0.45:
        return {"code": None}
    if roll < 0.65:
        scope = rng.choice(("connection", "model"))
        signal = {"scope": scope, "source": "typed:test"}
        if scope == "model":
            signal["model"] = model
        if rng.random() < 0.4:
            signal["reset_at"] = now + rng.uniform(-1.0, 5.0)
        return {"code": c.PLAN_LIMIT, "limit": signal}
    if roll < 0.8:
        return {"code": c.SHORT_FAULT}
    return {"code": rng.choice(("engine_error", "deadline", "auth_error"))}


def one_iteration(mod, c, rng, args, v: h.Violations, counters: dict) -> None:
    clock = FakeClock()
    breaker = mod.ConnectionBreaker(clock, **PARAMS)
    stamps = h.StampLock(hold=not args.mutant)
    breaker._lock = stamps
    history: list[tuple] = []
    hist_lock = threading.Lock()
    tokens: list[str] = []

    def call(name, *a, **kw):
        clock.local.reads = []
        result = getattr(breaker, name)(*a, **kw)
        stamp = stamps.take()
        v.check(stamp is not None, f"{name} returned without taking the breaker lock")
        with hist_lock:
            history.append((stamp, name, a, kw, list(clock.local.reads), result))
        return result

    def worker(seed):
        wrng = random.Random(seed)
        held: list[tuple] = []
        for _ in range(60):
            conn, model, tenant = wrng.choice(CONNECTIONS), wrng.choice(MODELS), wrng.choice(TENANTS)
            clock.advance(wrng.uniform(0.0, 0.3))
            roll = wrng.random()
            if roll < 0.45:
                # Half the admits take the production lease: the run's own deadline plus grace.
                deadline = None if wrng.random() < 0.5 else wrng.choice((1.0, 10.0))
                got = call("admit", conn, model, tenant, deadline_s=deadline)
                if got["admitted"] and got["probe"]:
                    held.append((conn, model, tenant, got["probe"]))
                    with hist_lock:
                        tokens.append(got["probe"])
            elif roll < 0.85:
                probe = None
                if held and wrng.random() < 0.7:
                    conn, model, tenant, probe = held.pop(wrng.randrange(len(held)))
                elif tokens and wrng.random() < 0.3:
                    probe = wrng.choice(tokens)  # a stale or someone else's token
                call("record", conn, model, tenant, outcome(wrng, c, conn, model, clock._now), probe=probe)
            elif roll < 0.92:
                call("reset", conn, wrng.choice((None, model)))
            else:
                call("state", conn, wrng.choice((None, model)))
            if wrng.random() < 0.05:
                held.clear()  # a probe holder that dies without reporting

    workers = [lambda s=rng.randrange(2**32): worker(s) for _ in range(args.threads)]
    for problem in h.hammer(workers):
        v.add(problem)

    history.sort(key=lambda e: e[0])
    stamps_seen = [e[0] for e in history]
    v.check(stamps_seen == list(range(1, len(history) + 1)), "stamps are not a gap-free sequence")

    replay_reads: list[float] = []

    def replay_clock() -> float:
        if not replay_reads:
            v.add("replay read the clock more often than the concurrent run")
            return 0.0
        return replay_reads.pop(0)

    seq = mod.ConnectionBreaker(replay_clock, **PARAMS)
    live: dict[str, tuple[str, float]] = {}  # key -> (token, lease_until)
    for stamp, name, a, kw, reads, result in history:
        replay_reads[:] = reads
        now = reads[0] if reads else None
        got = getattr(seq, name)(*a, **kw)
        v.check(not replay_reads, f"#{stamp} {name}: replay read fewer clock values than the run")
        if got != result:
            v.add(f"#{stamp} {name}{a}{kw}: concurrent {result!r} != sequential {got!r}")
        if name == "admit" and result["probe"]:
            key = result["key"]
            prior = live.get(key)
            if prior is not None and now is not None and now < prior[1]:
                v.add(f"#{stamp} second live probe on {key}: {prior[0]} (lease to {prior[1]}) "
                      f"then {result['probe']} at {now}")
            live[key] = (result["probe"], now + _lease_seconds(kw.get("deadline_s")))
            counters["probes"] += 1
        elif name == "record":
            probe = kw.get("probe")
            # A probe's lease ends when its holder reports it: closed, released, or (since #326
            # 09d0c36f) opened, where the limit may name a different key than the one the probe
            # leased and the lease is freed on whichever key held it.
            if probe and result in ("closed", "probe_released", "opened"):
                for key, (token, _) in list(live.items()):
                    if token == probe:
                        del live[key]
            if result == "opened":
                conn, model = a[0], a[1]
                signal = a[3]["limit"]
                key = conn if signal["scope"] == "connection" else mod.ConnectionBreaker.model_key(conn, signal["model"])
                live.pop(key, None)
                counters["opened"] += 1
        elif name == "reset":
            conn, model = a
            for key in list(live):
                if model is None and (key == conn or key.startswith(conn + "#model:")) \
                        or key == mod.ConnectionBreaker.model_key(conn, model or ""):
                    del live[key]
    v.check(seq._keys == breaker._keys, "final key state differs from sequential replay")
    v.check(seq._short == breaker._short, "final short-fault state differs from sequential replay")
    counters["ops"] += len(history)
    counters["refused"] += sum(1 for e in history if e[1] == "admit" and not e[5]["admitted"])


def main() -> int:
    args = h.parse_args()
    sys.path.insert(0, str(Path(args.tree) / "core"))
    from recursion_core.routes import breaker as mod
    from recursion_core.routes import contract as c

    started = time.monotonic()
    v = h.Violations()
    rng = random.Random(args.seed)
    counters = {"ops": 0, "probes": 0, "opened": 0, "refused": 0}
    for _ in range(args.iters):
        one_iteration(mod, c, rng, args, v, counters)
    return h.finish("breaker_326", args, v, started=started,
                    extra={"measured": h.tree_info(args.tree, "core/recursion_core/routes/breaker.py"),
                           "counters": counters})


if __name__ == "__main__":
    sys.exit(main())
