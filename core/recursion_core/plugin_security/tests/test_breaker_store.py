"""Round 1 finding 3: breaker trips, faults and run bindings survive a restart; leased probe.

Runs outlive a daemon restart (grill Q38), so losing breaker state would remove a refusal.
These tests need no signing, so they also run where ``cryptography`` is missing.
"""

from __future__ import annotations

import os
import threading
import unittest

from recursion_core.plugin_security import (
    ApprovalGate, ConnectionBreaker, CoreHealth, CoreService, PluginLoader, Refused, ReservedEffects,
    TrustStore, canonical, create_breaker_store, parse,
)
from recursion_core.plugin_security.tests.support import FakeGilProbe, authenticate, temp_dir


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class BreakerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(temp_dir(self), "breaker.db")
        create_breaker_store(self.path)
        self.clock = FakeClock()

    def _open(self) -> ConnectionBreaker:
        breaker = ConnectionBreaker(self.path, clock=self.clock, probe_lease_seconds=10)
        self.addCleanup(breaker.close)
        return breaker

    def _refused(self, code: str, fn, *args) -> None:
        with self.assertRaises(Refused) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.code, code)

    def test_restart_keeps_trip_fault_and_binding(self) -> None:
        before = self._open()
        before.bind_run("run-1", "conn-a", "demo.plugin", "prov", "m1")
        before.bind_run("run-2", "conn-b", "demo.plugin", "prov", "m1")
        before.fault_run("run-2")
        self.assertTrue(before.report("conn-a", "plan_limit"))
        before.close()

        after = self._open()  # a daemon restart: a new process opens the same store
        self.assertTrue(after.tripped("conn-a"))
        self._refused("connection_tripped", after.check, "run-1", "conn-a", "demo.plugin")
        self._refused("run_faulted", after.check, "run-2", "conn-b", "demo.plugin")
        # The host re-binding the same runs after restart does not clear anything.
        after.bind_run("run-1", "conn-a", "demo.plugin", "prov", "m1")
        after.bind_run("run-2", "conn-b", "demo.plugin", "prov", "m1")
        self._refused("connection_tripped", after.check, "run-1", "conn-a", "demo.plugin")
        self._refused("run_faulted", after.check, "run-2", "conn-b", "demo.plugin")
        # Nor can a run move to a healthy connection or to another owner.
        self._refused("run_connection_mismatch", after.bind_run, "run-1", "conn-b", "demo.plugin", "prov", "m1")
        self._refused("run_binding_mismatch", after.bind_run, "run-1", "conn-a", "other.plugin", "prov", "m1")

    def test_trip_clears_only_through_a_live_lease(self) -> None:
        breaker = self._open()
        breaker.bind_run("run-1", "conn-a", "demo.plugin", "prov", "m1")
        self._refused("connection_not_tripped", breaker.begin_probe, "conn-a")
        breaker.report("conn-a", "rate_limit")
        for lease in (None, "", "0" * 32, 7):
            self.assertFalse(breaker.reset_after_probe("conn-a", lease, True))
        lease = breaker.begin_probe("conn-a")
        self._refused("probe_lease_held", breaker.begin_probe, "conn-a")  # one probe at a time
        self.assertFalse(breaker.reset_after_probe("conn-b", lease, True))  # another connection's lease
        self.clock.now += 11
        self.assertFalse(breaker.reset_after_probe("conn-a", lease, True))  # expired
        self.assertTrue(breaker.tripped("conn-a"))
        lease = breaker.begin_probe("conn-a")
        self.assertFalse(breaker.reset_after_probe("conn-a", lease, False))  # failed probe spends the lease
        self.assertFalse(breaker.reset_after_probe("conn-a", lease, True))   # and it is one use
        lease = breaker.begin_probe("conn-a")
        breaker.report("conn-a", "plan_limit")  # a new trip while probing voids the lease
        self.assertFalse(breaker.reset_after_probe("conn-a", lease, True))
        lease = breaker.begin_probe("conn-a")
        self.assertTrue(breaker.reset_after_probe("conn-a", lease, True))
        self.assertFalse(breaker.tripped("conn-a"))
        self.assertEqual(breaker.check("run-1", "conn-a", "demo.plugin"),
                         {"provider": "prov", "model_version": "m1"})

    def test_restart_drops_the_lease_not_the_trip(self) -> None:
        before = self._open()
        before.report("conn-a", "plan_limit")
        lease = before.begin_probe("conn-a")
        before.close()
        after = self._open()
        self.assertFalse(after.reset_after_probe("conn-a", lease, True))
        self.assertTrue(after.tripped("conn-a"))

    def test_daemon_never_creates_the_store(self) -> None:
        self._refused("breaker_store_missing", ConnectionBreaker, self.path + ".gone")
        self.assertFalse(os.path.exists(self.path + ".gone"))
        self.assertRaises(FileExistsError, create_breaker_store, self.path)
        junk = os.path.join(temp_dir(self), "junk.db")
        with open(junk, "wb") as fh:
            fh.write(b"not sqlite")
        self._refused("breaker_store_invalid", ConnectionBreaker, junk)

    def test_concurrent_binds_agree_on_one_owner(self) -> None:
        breaker = self._open()
        results: list = []

        def bind(owner: str) -> None:
            try:
                breaker.bind_run("run-x", "conn-a", owner, "prov", "m1")
                results.append(owner)
            except Refused as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=bind, args=("p%d" % (i % 2),)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = {r for r in results if r != "run_binding_mismatch"}
        self.assertEqual(len(winners), 1, results)


class ProbeOverProtocolTests(unittest.TestCase):
    """Round 2 finding 2: a consumer holding only ``client_for`` clients can clear a trip,
    and only through the leased probe."""

    def setUp(self) -> None:
        path = os.path.join(temp_dir(self), "breaker.db")
        create_breaker_store(path)
        self.clock = FakeClock()
        self.breaker = ConnectionBreaker(path, clock=self.clock, probe_lease_seconds=10)
        self.addCleanup(self.breaker.close)
        health = CoreHealth()
        loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), health, FakeGilProbe())
        core = CoreService(loader=loader, gate=ApprovalGate(lambda *a: None, authenticate, self.clock),
                           breaker=self.breaker, health=health, authority=None,
                           effect_permissions={}, authority_required=frozenset())
        self.host = core.client_for("host")
        self.plugin = core.client_for("demo.plugin")

    def _call(self, client, op: str, **args) -> dict:
        return parse(client(canonical({"protocol": "csl-core/v0", "op": op, "args": args})))

    def _code(self, out: dict):
        return None if out["ok"] else out["refusal"]["code"]

    def test_trip_clears_only_through_host_probe_ops(self) -> None:
        self.assertEqual(self._code(self._call(self.host, "breaker.probe_begin", connection="conn-a")),
                         "connection_not_tripped")
        self.assertTrue(self._call(self.host, "breaker.report", connection="conn-a",
                                   event_type="rate_limit")["result"]["tripped"])
        for op, args in (("breaker.probe_begin", dict(connection="conn-a")),
                         ("breaker.probe_reset", dict(connection="conn-a", lease="x", probe_ok=True))):
            self.assertEqual(self._code(self._call(self.plugin, op, **args)), "host_only")
        lease = self._call(self.host, "breaker.probe_begin", connection="conn-a")["result"]["lease"]
        self.assertEqual(self._code(self._call(self.host, "breaker.probe_begin", connection="conn-a")),
                         "probe_lease_held")
        # A wrong lease, or a failed probe, leaves the trip in place.
        self.assertFalse(self._call(self.host, "breaker.probe_reset", connection="conn-a", lease="nope",
                                    probe_ok=True)["result"]["reset"])
        self.assertFalse(self._call(self.host, "breaker.probe_reset", connection="conn-a", lease=lease,
                                    probe_ok=False)["result"]["reset"])
        self.assertTrue(self.breaker.tripped("conn-a"))
        lease = self._call(self.host, "breaker.probe_begin", connection="conn-a")["result"]["lease"]
        self.assertEqual(self._code(self._call(self.host, "breaker.probe_reset", connection=["conn-a"],
                                               lease=lease, probe_ok=True)), "invalid_connection")
        self.assertTrue(self._call(self.host, "breaker.probe_reset", connection="conn-a", lease=lease,
                                   probe_ok=True)["result"]["reset"])
        self.assertFalse(self.breaker.tripped("conn-a"))
        # One use: the spent lease clears nothing a second time.
        self._call(self.host, "breaker.report", connection="conn-a", event_type="rate_limit")
        self.assertFalse(self._call(self.host, "breaker.probe_reset", connection="conn-a", lease=lease,
                                    probe_ok=True)["result"]["reset"])


if __name__ == "__main__":
    unittest.main()
