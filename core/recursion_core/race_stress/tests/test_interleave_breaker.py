"""Adversarial interleavings on both connection-keyed breakers - CSL-801 L11b.

* recursion #326 ``routes.breaker.ConnectionBreaker`` (CSL-797): half-open probe leasing.
  Invariants: one probe per key at a time even when N callers arrive the instant a cooldown
  or a lease ends; a probe token from before a ``reset`` or a lease handover cannot close
  the key it no longer owns; untyped failures never open a key, however many race.
* recursion #328 ``plugin_security.breaker.ConnectionBreaker`` (O11, B15): a run binds one
  connection once; a fault or a trip, once returned, is seen by every later ``check``; a
  reset with no leased probe, or with a lease a newer trip dropped, does not clear a trip.
  #328 has a probe lease since ``afe38c1f`` (``begin_probe``); these were ``expectedFailure``
  at ``0264628b`` and now pass. The stress form of the stale-probe race (CSL-804) is in
  ``test_race_stress.py`` and ``drivers/repro_328_stale_probe.py``. The #326 cross-scope
  lease gap (a model-scoped limit from a connection probe fenced other models) is fixed at
  ``09d0c36f`` and now asserted as a plain test.

The clock is a shared fake, so "the instant the cooldown ends" is exact rather than hoped for.
Needs ``RACE_STRESS_TREE_326`` / ``RACE_STRESS_TREE_328``. Run under ``python3.14t -X gil=0``.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest

from recursion_core.race_stress.tests.interleave_support import (
    ITERATIONS, THREADS, SeededCase, load_stacked, run_threads)

CONN = "api_key:k1"
MODEL = "m1"


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self._now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, dt: float) -> None:
        with self._lock:
            self._now += dt


class RoutesBreakerCase(SeededCase):
    def setUp(self) -> None:
        self.mod = load_stacked("RACE_STRESS_TREE_326", "recursion_core.routes.breaker")
        self.c = load_stacked("RACE_STRESS_TREE_326", "recursion_core.routes.contract")
        self.clock = Clock()
        self.br = self.mod.ConnectionBreaker(self.clock, cooldown_s=10, max_cooldown_s=80,
                                             probe_lease_s=5, short_fault_s=3)

    def limit(self, scope="connection"):
        return {"code": self.c.PLAN_LIMIT,
                "limit": self.c.limit_signal(scope, "typed:test", model=MODEL if scope == "model" else None)}

    def stampede(self, tenants=None) -> list[dict]:
        out: list[dict] = []
        lock = threading.Lock()

        def admit(t):
            res = self.br.admit(CONN, MODEL, t)
            with lock:
                out.append(res)

        tenants = tenants or [f"t{i}" for i in range(THREADS)]
        errors = run_threads([lambda t=t: admit(t) for t in tenants])
        self.assertEqual(errors, [])
        return out


class HalfOpenStampedeTests(RoutesBreakerCase):
    def test_exactly_one_probe_when_cooldown_ends_under_a_stampede(self) -> None:
        for i in range(ITERATIONS):
            self.br.reset(CONN)
            self.assertEqual(self.br.record(CONN, MODEL, "t0", self.limit()), "opened")
            self.clock.advance(10)  # exactly open_until: cooldown over
            out = self.stampede()
            probes = [r for r in out if r["probe"]]
            self.assertEqual(len(probes), 1, f"iteration {i}: {len(probes)} probes leased")
            self.assertTrue(all(r["code"] == self.c.PROBE_IN_FLIGHT for r in out if not r["admitted"]))
            self.assertEqual(sum(r["admitted"] for r in out), 1)

    def test_lease_handover_stampede_and_late_report_of_the_expired_probe(self) -> None:
        for i in range(ITERATIONS):
            self.br.reset(CONN)
            self.br.record(CONN, MODEL, "t0", self.limit())
            self.clock.advance(10)
            first = self.br.admit(CONN, MODEL, "t0")["probe"]
            self.assertIsNotNone(first)
            self.clock.advance(5)  # first probe's lease just ended
            out = self.stampede()
            second = [r["probe"] for r in out if r["probe"]]
            self.assertEqual(len(second), 1, f"iteration {i}: lease handed to {len(second)} probes")
            # The expired probe reports success late, racing a fresh stampede. It no longer owns
            # the key, so it must not close it; the live probe still holds the lease.
            late: list[str] = []
            errors = run_threads([lambda: late.append(self.br.record(CONN, MODEL, "t0", {"code": None}, first))]
                                 + [lambda t=t: self.br.admit(CONN, MODEL, t) for t in ("x1", "x2", "x3")])
            self.assertEqual(errors, [])
            self.assertEqual(late, ["none"])
            self.assertEqual(self.br.state(CONN), self.mod.HALF_OPEN)
            self.assertEqual(self.br.admit(CONN, MODEL, "t9")["code"], self.c.PROBE_IN_FLIGHT)
            self.assertEqual(self.br.record(CONN, MODEL, "t0", {"code": None}, second[0]), "closed")
            self.assertEqual(self.br.state(CONN), self.mod.CLOSED)

    def test_probe_from_before_reset_cannot_close_a_reopened_key(self) -> None:
        """All six orders of {operator reset, new typed limit, stale probe success}.

        The stale probe was leased before the reset. The key ends open exactly when the new
        limit lands after the reset; the stale success may never close a key it was not
        leased on.
        """
        import itertools
        for order in itertools.permutations(("reset", "limit", "stale")):
            with self.subTest(order=order):
                self.br.reset(CONN)
                self.br.record(CONN, MODEL, "t0", self.limit())
                self.clock.advance(10)
                stale = self.br.admit(CONN, MODEL, "t0")["probe"]
                self.assertIsNotNone(stale)
                ops = {"reset": lambda: self.br.reset(CONN),
                       "limit": lambda: self.br.record(CONN, MODEL, "t1", self.limit()),
                       "stale": lambda: self.br.record(CONN, MODEL, "t0", {"code": None}, stale)}
                for name in order:
                    ops[name]()
                expect_open = order.index("limit") > order.index("reset")
                self.assertEqual(self.br.state(CONN), self.mod.OPEN if expect_open else self.mod.CLOSED)
        for i in range(ITERATIONS):  # and raced, for errors only: any order above is legal
            self.br.reset(CONN)
            self.br.record(CONN, MODEL, "t0", self.limit())
            self.clock.advance(10)
            stale = self.br.admit(CONN, MODEL, "t0")["probe"]
            errors = run_threads([lambda: self.br.reset(CONN),
                                  lambda: self.br.record(CONN, MODEL, "t1", self.limit()),
                                  lambda: self.br.record(CONN, MODEL, "t0", {"code": None}, stale)])
            self.assertEqual(errors, [], f"iteration {i}")
            self.assertIn(self.br.state(CONN), (self.mod.OPEN, self.mod.CLOSED))

    def test_limit_landing_mid_probe_survives_the_probe_success(self) -> None:
        """The #328 stale-probe interleaving, on #326: a newer typed limit beats an older probe.

        Probe leased; another run reports a typed limit while the probe is out; then the probe
        reports success. The newest evidence is the limit, so the key must stay open. Also
        raced: the limit and the probe success arrive together; whichever lands last decides,
        and a success that lands after the limit may not close it.
        """
        for scope in ("connection", "model"):
            with self.subTest(scope=scope):
                self.br.reset(CONN)
                self.br.record(CONN, MODEL, "t0", self.limit(scope))
                self.clock.advance(80)
                probe = self.br.admit(CONN, MODEL, "t0")["probe"]
                self.assertIsNotNone(probe)
                self.assertEqual(self.br.record(CONN, MODEL, "t1", self.limit(scope)), "opened")
                self.assertEqual(self.br.record(CONN, MODEL, "t0", {"code": None}, probe), "none")
                key_model = None if scope == "connection" else MODEL
                self.assertEqual(self.br.state(CONN, key_model), self.mod.OPEN)
        for i in range(ITERATIONS):
            self.br.reset(CONN)
            self.br.record(CONN, MODEL, "t0", self.limit())
            self.clock.advance(80)
            probe = self.br.admit(CONN, MODEL, "t0")["probe"]
            effects: list[str] = []
            errors = run_threads([lambda: effects.append(self.br.record(CONN, MODEL, "t1", self.limit())),
                                  lambda: effects.append(self.br.record(CONN, MODEL, "t0", {"code": None}, probe))])
            self.assertEqual(errors, [])
            # "closed" first then "opened" -> open; "opened" first -> the probe finds nothing.
            self.assertEqual(self.br.state(CONN), self.mod.OPEN, f"iteration {i}: {effects}")

    def test_model_limit_from_a_connection_probe_leaves_other_models_available(self) -> None:
        """PRODUCT.md 055cc7a5 3.4 S3: per model only where the vendor meters per model.

        The connection key is open and its cooldown is over, so admitting (CONN, m1) leases the
        *connection* key's probe. That probe ends with a model-scoped PLAN_LIMIT: the vendor
        metered m1's own bucket, so m1 opens and the connection itself is not limited. Another
        model on the same connection must then be admitted. #326 at 82247cbc opened the m1 key
        but kept the connection key's lease, so m2 was refused ``probe_in_flight`` until
        ``probe_lease_s`` ran out (150 s by default). Fixed at 09d0c36f (``_open`` releases the
        probe on whichever key held it); this was ``expectedFailure`` until then.
        """
        self.br.reset(CONN)
        self.assertEqual(self.br.record(CONN, MODEL, "t0", self.limit()), "opened")
        self.clock.advance(10)  # cooldown over
        res = self.br.admit(CONN, MODEL, "t0")
        self.assertEqual(res["key"], CONN, "the probe should lease the connection key")
        self.assertIsNotNone(res["probe"])
        self.assertEqual(self.br.record(CONN, MODEL, "t0", self.limit("model"), res["probe"]), "opened")
        self.assertEqual(self.br.state(CONN, MODEL), self.mod.OPEN)
        self.assertFalse(self.br.admit(CONN, MODEL, "t1")["admitted"])  # m1 itself stays fenced
        other = self.br.admit(CONN, "m2", "t1")
        self.assertTrue(other["admitted"], f"m2 on {CONN} refused after an m1-only limit: {other}")

    def test_untyped_failures_never_open_however_many_race(self) -> None:
        codes = [code for code in self.c.CODES if code not in (self.c.PLAN_LIMIT, self.c.SHORT_FAULT)]

        def spam(salt):
            rng = self.rng(str(salt))
            for _ in range(200):
                code = rng.choice(codes)
                # A PLAN_LIMIT without a typed signal is also untyped.
                outcome = {"code": self.c.PLAN_LIMIT, "limit": {"scope": "connection"}} \
                    if rng.random() < 0.2 else {"code": code, "limit": self.c.limit_signal("connection", "x")}
                self.br.record(CONN, MODEL, f"t{salt}", outcome)

        self.assertEqual(run_threads([lambda s=s: spam(s) for s in range(THREADS)]), [])
        self.assertEqual(self.br.state(CONN), self.mod.CLOSED)
        self.assertTrue(self.br.admit(CONN, MODEL, "t0")["admitted"])

    def test_randomized_mix_keeps_one_probe_per_key(self) -> None:
        """Random admit/record/reset/clock traffic; a monitor checks one live lease per key."""
        violations: list[str] = []
        stop = threading.Event()

        def monitor():
            while not stop.is_set():
                with self.br._lock:  # snapshot under the breaker's own lock
                    now = self.clock()
                    for key, rec in self.br._keys.items():
                        if rec["state"] == self.mod.HALF_OPEN and rec["probe"] and rec["lease_until"] is None:
                            violations.append(f"{key}: probe without lease")
                        if rec["state"] == self.mod.OPEN and rec.get("probe"):
                            violations.append(f"{key}: open with a probe")
                        if rec["state"] == self.mod.OPEN and not now < rec["open_until"] + 10_000:
                            violations.append(f"{key}: open_until unbounded")

        def worker(salt):
            rng = self.rng(f"mix:{salt}")
            held: list[tuple[str, str]] = []
            for _ in range(300):
                model = rng.choice(("m1", "m2"))
                r = rng.random()
                if r < 0.45:
                    res = self.br.admit(CONN, model, f"t{salt}")
                    if res["probe"]:
                        held.append((model, res["probe"]))
                elif r < 0.85:
                    model, probe = held.pop() if held and rng.random() < 0.7 else (model, None)
                    outcome = rng.choice([{"code": None}, {"code": self.c.SHORT_FAULT}, self.limit(),
                                          self.limit("model"), {"code": rng.choice(sorted(self.c.CODES))}])
                    self.br.record(CONN, model, f"t{salt}", outcome, probe)
                elif r < 0.9:
                    self.br.reset(CONN, rng.choice((None, "m1")))
                else:
                    self.clock.advance(rng.choice((0.5, 3, 5, 10)))

        t = threading.Thread(target=monitor, daemon=True)
        t.start()
        try:
            errors = run_threads([lambda s=s: worker(s) for s in range(THREADS)])
        finally:
            stop.set()
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(violations[:5], [])
        # Always recovers: after max cooldown and one successful probe per key, all closed.
        self.clock.advance(10_000)
        for _ in range(4):
            res = self.br.admit(CONN, "m1", "t0")
            if res["probe"]:
                self.br.record(CONN, "m1", "t0", {"code": None}, res["probe"])
        self.assertEqual(self.br.state(CONN), self.mod.CLOSED)
        self.assertTrue(self.br.admit(CONN, "m1", "t0")["admitted"])


class PluginBreakerCase(SeededCase):
    PLUGIN = "plug-1"

    def setUp(self) -> None:
        self.mod = load_stacked("RACE_STRESS_TREE_328", "recursion_core.plugin_security.breaker")
        tmp = tempfile.TemporaryDirectory(prefix="rs328-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "breaker.db")
        self.mod.create_breaker_store(path)  # the installer's step; the breaker never creates it
        self.clock = Clock()
        self.br = self.mod.ConnectionBreaker(path, clock=self.clock, probe_lease_seconds=60.0)
        self.addCleanup(self.br.close)

    def bind(self, run, conn):
        return self.br.bind_run(run, conn, self.PLUGIN, "prov", "model-1")

    def check(self, run, conn):
        return self.br.check(run, conn, self.PLUGIN)

    def code_of(self, fn, *args):
        try:
            fn(*args)
        except self.mod.Refused as exc:
            return exc.args[0] if exc.args else str(exc)
        return None


class PluginBreakerTests(PluginBreakerCase):
    def test_concurrent_bind_of_one_run_to_many_connections_keeps_one(self) -> None:
        for i in range(ITERATIONS):
            run = f"run-{i}"
            codes: dict[str, object] = {}
            lock = threading.Lock()

            def bind(conn):
                code = self.code_of(self.bind, run, conn)
                with lock:
                    codes[conn] = code

            conns = [f"conn-{n}" for n in range(THREADS)]
            self.assertEqual(run_threads([lambda c=c: bind(c) for c in conns]), [])
            winners = [c for c, code in codes.items() if code is None]
            self.assertEqual(len(winners), 1, f"iteration {i}: {codes}")
            self.assertTrue(all(codes[c] == "run_connection_mismatch" for c in conns if c != winners[0]))
            self.assertIsNone(self.code_of(self.check, run, winners[0]))
            for c in conns:
                if c != winners[0]:
                    self.assertEqual(self.code_of(self.check, run, c), "run_connection_mismatch")

    def test_fault_and_trip_are_monotonic_for_every_later_check(self) -> None:
        for i in range(ITERATIONS):
            run, conn = f"run-{i}", f"conn-{i}"
            self.bind(run, conn)
            faulted = threading.Event()
            tripped = threading.Event()
            late_pass: list[str] = []

            def checker():
                for _ in range(500):
                    was_faulted, was_tripped = faulted.is_set(), tripped.is_set()
                    code = self.code_of(self.check, run, conn)
                    if (was_faulted or was_tripped) and code is None:
                        late_pass.append(f"passed after fault={was_faulted} trip={was_tripped}")

            def faulter():
                self.br.fault_run(run)
                faulted.set()

            def tripper():
                self.assertTrue(self.br.report(conn, "plan_limit"))
                tripped.set()

            errors = run_threads([checker] * (THREADS - 2) + [faulter, tripper])
            self.assertEqual(errors, [])
            self.assertEqual(late_pass, [], f"iteration {i}")
            self.assertTrue(self.br.tripped(conn))

    def test_reset_without_a_leased_probe_does_not_clear_a_trip(self) -> None:
        """PRODUCT.md 055cc7a5 3.4 S3 ("a leased probe resets it") and the engine contract,
        "Report a plan limit" item 4 ("the leased probe resets it when the limit clears").

        A connection is tripped by a typed limit and no probe is leased. A reset with no lease,
        a made-up lease, or a non-string lease is the caller's word that some probe succeeded,
        and it must not clear the trip. This was ``expectedFailure`` at #328 0264628b (no lease
        API); #328 afe38c1f ties the reset to a live lease from ``begin_probe``.
        """
        run, conn = "r-unleased", "c-unleased"
        self.bind(run, conn)
        self.assertTrue(self.br.report(conn, "plan_limit"))
        for lease in (None, "0" * 32, True, 1):
            with self.subTest(lease=lease):
                self.assertFalse(self.br.reset_after_probe(conn, lease, True))
                self.assertTrue(self.br.tripped(conn))
                self.assertEqual(self.code_of(self.check, run, conn), "connection_tripped")
        # A real lease from begin_probe does clear it, once.
        lease = self.br.begin_probe(conn)
        self.assertTrue(self.br.reset_after_probe(conn, lease, True))
        self.assertFalse(self.br.tripped(conn))
        self.assertIsNone(self.code_of(self.check, run, conn))
        self.assertFalse(self.br.reset_after_probe(conn, lease, True))

    def test_lease_dropped_by_a_newer_trip_cannot_clear_it(self) -> None:
        """CSL-804 in its deterministic form: lease, probe ends, a newer limit lands, the old
        probe reports success. The newer trip must stand (it dropped the lease)."""
        run, conn = "r-stale", "c-stale"
        self.bind(run, conn)
        self.assertTrue(self.br.report(conn, "rate_limit"))
        lease = self.br.begin_probe(conn)
        self.assertTrue(self.br.report(conn, "plan_limit"))
        self.assertFalse(self.br.reset_after_probe(conn, lease, True))
        self.assertTrue(self.br.tripped(conn))
        self.assertEqual(self.code_of(self.check, run, conn), "connection_tripped")

    def test_one_live_lease_per_connection_under_a_stampede(self) -> None:
        for i in range(ITERATIONS):
            conn = f"c-stampede-{i}"
            self.assertTrue(self.br.report(conn, "plan_limit"))
            got: list[object] = []
            lock = threading.Lock()

            def grab():
                code = None
                try:
                    lease = self.br.begin_probe(conn)
                except self.mod.Refused as exc:
                    lease, code = None, exc.args[0] if exc.args else str(exc)
                with lock:
                    got.append(lease or code)

            self.assertEqual(run_threads([grab] * THREADS), [])
            leases = [g for g in got if g != "probe_lease_held"]
            self.assertEqual(len(leases), 1, f"iteration {i}: {got}")
            # An expired lease cannot reset; the next begin_probe may lease again.
            self.clock.advance(61)
            self.assertFalse(self.br.reset_after_probe(conn, leases[0], True))
            self.assertTrue(self.br.tripped(conn))
            self.assertTrue(self.br.reset_after_probe(conn, self.br.begin_probe(conn), True))

    def test_report_reset_race_leaves_tripped_and_check_consistent(self) -> None:
        """``tripped`` and ``check`` agree after report / leased-probe-reset flapping."""
        for i in range(ITERATIONS):
            run, conn = f"r{i}", f"c{i}"
            self.bind(run, conn)

            def flap(salt):
                rng = self.rng(f"{i}:{salt}")
                for _ in range(100):
                    if rng.random() < 0.5:
                        self.br.report(conn, rng.choice(sorted(self.mod.TYPED_LIMITS)))
                    else:
                        try:
                            lease = self.br.begin_probe(conn)
                        except self.mod.Refused:
                            continue
                        self.br.reset_after_probe(conn, lease, rng.random() < 0.8)
                    # Non-trip events race too and must never trip.
                    self.assertFalse(self.br.report(conn, "core_deadline"))

            self.assertEqual(run_threads([lambda s=s: flap(s) for s in range(THREADS)]), [])
            code = self.code_of(self.check, run, conn)
            self.assertEqual(code == "connection_tripped", self.br.tripped(conn), f"iteration {i}")

if __name__ == "__main__":
    unittest.main()
