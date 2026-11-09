"""Connection-keyed breaker, driven directly (CSL-797). Lesson: CSL-608."""

from __future__ import annotations

import threading
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.tests.fakes import FakeClock

LIMIT = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "test.typed")}


class BreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.b = ConnectionBreaker(self.clock, cooldown_s=300, max_cooldown_s=3600, probe_lease_s=150, short_fault_s=60)

    def test_plan_limit_fences_every_tenant_on_that_connection_and_no_other(self) -> None:
        self.assertEqual(self.b.record("subscription:A", "m", "t1", LIMIT), "opened")
        self.assertEqual(self.b.admit("subscription:A", "m", "t2")["code"], c.BREAKER_OPEN)
        self.assertEqual(self.b.admit("subscription:A", "other-model", "t1")["code"], c.BREAKER_OPEN)
        self.assertTrue(self.b.admit("subscription:B", "m", "t1")["admitted"])
        self.assertTrue(self.b.admit("api_key:K", "m", "t2")["admitted"])

    def test_non_limit_outcomes_never_open(self) -> None:
        for code in (c.DEADLINE_EXCEEDED, c.ENGINE_ERROR, c.AUTH_FAILED, c.PROTOCOL_ERROR, c.ENGINE_EXITED):
            for _ in range(50):
                self.assertEqual(self.b.record("subscription:A", "m", "t1", {"code": code}), "none")
        self.assertEqual(self.b.state("subscription:A"), "closed")
        self.assertTrue(self.b.admit("subscription:A", "m", "t2")["admitted"])
        self.assertTrue(self.b.admit("subscription:A", "m", "t1")["admitted"])

    def test_plan_limit_without_typed_signal_does_not_open(self) -> None:
        for bad in (None, "rate limit", {"scope": "everything"}, {"scope": "connection"}):
            self.assertEqual(self.b.record("subscription:A", "m", "t1", {"code": c.PLAN_LIMIT, "limit": bad}), "none")
        self.assertEqual(self.b.state("subscription:A"), "closed")

    def test_short_fault_is_one_model_one_tenant_and_brief(self) -> None:
        self.assertEqual(self.b.record("subscription:A", "m", "t1", {"code": c.SHORT_FAULT}), "short_fault")
        self.assertEqual(self.b.admit("subscription:A", "m", "t1")["code"], c.SHORT_FAULT)
        self.assertTrue(self.b.admit("subscription:A", "m", "t2")["admitted"])
        self.assertTrue(self.b.admit("subscription:A", "m2", "t1")["admitted"])
        self.assertEqual(self.b.state("subscription:A"), "closed")
        self.clock.advance(61)
        self.assertTrue(self.b.admit("subscription:A", "m", "t1")["admitted"])

    def test_model_scoped_limit_fences_only_that_model(self) -> None:
        sig = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("model", "test.bucket", model="gpt-x")}
        self.b.record("api_key:K", "gpt-x", "t1", sig)
        self.assertEqual(self.b.admit("api_key:K", "gpt-x", "t2")["code"], c.BREAKER_OPEN)
        self.assertTrue(self.b.admit("api_key:K", "gpt-y", "t2")["admitted"])

    def test_recovery_single_leased_probe_then_closed(self) -> None:
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(299)
        self.assertEqual(self.b.admit("subscription:A", "m", "t1")["code"], c.BREAKER_OPEN)
        self.clock.advance(1)
        first = self.b.admit("subscription:A", "m", "t1")
        self.assertTrue(first["admitted"])
        self.assertIsNotNone(first["probe"])
        self.assertEqual(self.b.admit("subscription:A", "m", "t2")["code"], c.PROBE_IN_FLIGHT)
        self.assertEqual(self.b.record("subscription:A", "m", "t1", {"code": None}, first["probe"]), "closed")
        self.assertEqual(self.b.state("subscription:A"), "closed")
        nxt = self.b.admit("subscription:A", "m", "t2")
        self.assertTrue(nxt["admitted"])
        self.assertIsNone(nxt["probe"])

    def test_probe_lease_expires(self) -> None:
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(300)
        lost = self.b.admit("subscription:A", "m", "t1")["probe"]
        self.clock.advance(149)
        self.assertEqual(self.b.admit("subscription:A", "m", "t2")["code"], c.PROBE_IN_FLIGHT)
        self.clock.advance(1)
        second = self.b.admit("subscription:A", "m", "t2")
        self.assertTrue(second["admitted"])
        self.assertNotEqual(second["probe"], lost)
        self.assertEqual(self.b.record("subscription:A", "m", "t1", {"code": None}, lost), "none")  # stale probe
        self.assertEqual(self.b.state("subscription:A"), "half_open")

    def test_probe_lease_covers_the_probe_deadline_plus_grace(self) -> None:
        # 11.2 C1-18: the lease is bounded at the probe run's deadline plus 30 s, so a long probe still
        # inside its deadline is never joined by a second probe on the same connection.
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(300)
        first = self.b.admit("subscription:A", "m", "t1", deadline_s=600)["probe"]
        self.assertIsNotNone(first)
        self.clock.advance(629)
        self.assertEqual(self.b.admit("subscription:A", "m", "t2", deadline_s=600)["code"], c.PROBE_IN_FLIGHT)
        self.assertEqual(self.b.record("subscription:A", "m", "t1", {"code": None}, first), "closed")
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(600)
        short = self.b.admit("subscription:A", "m", "t1", deadline_s=10)
        self.clock.advance(39)
        self.assertEqual(self.b.admit("subscription:A", "m", "t2", deadline_s=10)["code"], c.PROBE_IN_FLIGHT)
        self.clock.advance(1)
        self.assertNotEqual(self.b.admit("subscription:A", "m", "t2", deadline_s=10)["probe"], short["probe"])

    def test_short_fault_refusal_is_not_breaker_open(self) -> None:
        self.b.record("subscription:A", "m", "t1", {"code": c.SHORT_FAULT})
        refused = self.b.admit("subscription:A", "m", "t1")
        self.assertEqual((refused["code"], refused["key"]), (c.SHORT_FAULT, "short_fault:m"))
        self.assertEqual(self.b.state("subscription:A"), "closed")

    def test_short_fault_never_hides_a_connection_limit(self) -> None:
        # t1's short fault on m, then the connection hits a typed limit: t1 sees the limit too, with the
        # vendor's reset time, not the end of its own short fault.
        self.b.record("subscription:A", "m", "t1", {"code": c.SHORT_FAULT})
        reset_at = self.clock.now + 900
        limit = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "test.typed", reset_at=reset_at)}
        self.assertEqual(self.b.record("subscription:A", "m", "t2", limit), "opened")
        for tenant in ("t1", "t2"):
            refused = self.b.admit("subscription:A", "m", tenant)
            self.assertEqual((refused["code"], refused["key"], refused["retry_at"]),
                             (c.BREAKER_OPEN, "subscription:A", reset_at), tenant)
        # Once the cooldown is over, t1's own short fault takes no probe lease; t2 probes, and t1 then sees
        # the probe in flight, not its short fault.
        self.b.reset("subscription:A")
        self.b.record("subscription:A", "m", "t2", LIMIT)
        self.clock.advance(300)
        self.b.record("subscription:A", "m", "t1", {"code": c.SHORT_FAULT})
        self.assertEqual(self.b.admit("subscription:A", "m", "t1")["code"], c.SHORT_FAULT)
        self.assertIsNotNone(self.b.admit("subscription:A", "m", "t2")["probe"])
        self.assertEqual(self.b.admit("subscription:A", "m", "t1")["code"], c.PROBE_IN_FLIGHT)

    def test_probe_failure_reopens_with_backoff(self) -> None:
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(300)
        probe = self.b.admit("subscription:A", "m", "t1")["probe"]
        self.assertEqual(self.b.record("subscription:A", "m", "t1", LIMIT, probe), "opened")
        self.clock.advance(599)
        self.assertEqual(self.b.admit("subscription:A", "m", "t1")["code"], c.BREAKER_OPEN)
        self.clock.advance(1)
        self.assertTrue(self.b.admit("subscription:A", "m", "t1")["admitted"])

    def test_probe_deadline_releases_lease_without_closing(self) -> None:
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(300)
        probe = self.b.admit("subscription:A", "m", "t1")["probe"]
        self.assertEqual(
            self.b.record("subscription:A", "m", "t1", {"code": c.DEADLINE_EXCEEDED}, probe), "probe_released")
        self.assertIsNotNone(self.b.admit("subscription:A", "m", "t2")["probe"])

    def test_backoff_is_capped_and_never_stays_open_forever(self) -> None:
        for _ in range(30):
            self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(3600)
        self.assertTrue(self.b.admit("subscription:A", "m", "t1")["admitted"])

    def test_vendor_reset_time_is_used_and_capped(self) -> None:
        sig = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "t", reset_at=self.clock() + 5000)}
        self.b.record("subscription:A", "m", "t1", sig)
        self.clock.advance(4999)
        self.assertEqual(self.b.admit("subscription:A", "m", "t1")["code"], c.BREAKER_OPEN)
        self.clock.advance(1)
        self.assertTrue(self.b.admit("subscription:A", "m", "t1")["admitted"])
        far = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "t", reset_at=self.clock() + 10**12)}
        self.b.record("subscription:B", "m", "t1", far)
        self.clock.advance(8 * 24 * 3600)
        self.assertTrue(self.b.admit("subscription:B", "m", "t1")["admitted"])

    def test_explicit_reset(self) -> None:
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.b.record("subscription:A", "m", "t1", {"code": c.SHORT_FAULT})
        self.b.reset("subscription:A")
        self.assertEqual(self.b.state("subscription:A"), "closed")
        res = self.b.admit("subscription:A", "m", "t1")
        self.assertTrue(res["admitted"])
        self.assertIsNone(res["probe"])

    def test_concurrent_admits_grant_exactly_one_probe(self) -> None:
        self.b.record("subscription:A", "m", "t1", LIMIT)
        self.clock.advance(300)
        results, start = [], threading.Barrier(16)

        def worker(i: int) -> None:
            start.wait()
            results.append(self.b.admit("subscription:A", "m", f"t{i}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r["probe"]), 1)
        self.assertEqual(sum(1 for r in results if r["code"] == c.PROBE_IN_FLIGHT), 15)


if __name__ == "__main__":
    unittest.main()
