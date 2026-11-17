"""Settled pairing bounds (lane REMOTE): 30 days, 7 days unused, a 60-second assertion, watch and Stop.

The clock is moved by hand (``support.FakeClock``). Nothing in this file reads the wall clock.
Record: 20261115-csl238-harness-dr-09-phone-pairing-01.
"""

from __future__ import annotations

import threading
import unittest

from recursion_core.remote import pairing
from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant, PairingRefused, device_scopes
from recursion_core.remote.tests import support

# Literals, not the module constants: changing a constant has to turn these red.
DAY_S = 24 * 60 * 60
MAX_AGE_S = 30 * DAY_S
UNUSED_S = 7 * DAY_S
ASSERTION_S = 60


def grant(device_id: str, scopes=("read",)) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", tuple(scopes), 0.0)


class HookClock(support.FakeClock):
    """Runs ``armed`` once, on the next reading, from inside the caller's lock."""

    armed = None

    def __call__(self) -> float:
        hook, self.armed = self.armed, None
        if hook is not None:
            hook()
        return self.now


class SettledValueTests(unittest.TestCase):
    def test_the_settled_values_are_named_in_one_block(self) -> None:
        self.assertEqual(getattr(pairing, "PAIRING_MAX_AGE_S", None), MAX_AGE_S)
        self.assertEqual(getattr(pairing, "PAIRING_UNUSED_AGE_S", None), UNUSED_S)
        self.assertEqual(getattr(pairing, "DANGEROUS_ASSERTION_TTL_S", None), ASSERTION_S)
        self.assertEqual(pairing.DEVICE_SCOPES, ("read", "stop"))
        self.assertEqual(getattr(pairing, "DANGEROUS_ACTIONS", None), ("run", "approve"))
        with open(pairing.__file__, encoding="utf-8") as source:
            text = source.read()
        self.assertIn("20261115-csl238-harness-dr-09-phone-pairing-01", text)
        # One assignment each, so the values are not copied to a second place.
        for name in ("PAIRING_MAX_AGE_S", "PAIRING_UNUSED_AGE_S", "DANGEROUS_ASSERTION_TTL_S",
                     "DEVICE_SCOPES", "DANGEROUS_ACTIONS"):
            self.assertEqual(text.count(f"\n{name} ="), 1, name)


class ScopeTests(unittest.TestCase):
    def test_a_passkeyless_device_is_granted_only_read_and_stop(self) -> None:
        self.assertEqual(device_scopes(["stop", "read"]), ("read", "stop"))
        self.assertEqual(device_scopes(("read", "stop")), ("read", "stop"))
        for scopes in (["run"], ["approve"], ["read", "run"], ["read", "approve"], ["stop", "approve"]):
            with self.subTest(scopes=scopes), self.assertRaises(PairingRefused) as caught:
                device_scopes(scopes)
            self.assertEqual(caught.exception.code, "scope_refused")

    def test_an_offer_for_run_or_approve_is_refused(self) -> None:
        book = pairing.OfferBook(clock=support.FakeClock())
        for scopes in (["run"], ["approve"], ["read", "run"]):
            with self.subTest(scopes=scopes):
                with self.assertRaises(PairingRefused) as caught:
                    book.issue(scopes=scopes, label="phone")
                self.assertEqual(caught.exception.code, "scope_refused")
        self.assertEqual(book.issue(scopes=["read", "stop"], label="phone").scopes, ("read", "stop"))


class LifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = support.FakeClock()
        self.book = DeviceBook(clock=self.clock)

    def add(self, device_id: str, *, expires_at: float | None = None) -> None:
        # A caller may ask for longer than 30 days. The device must still be refused at the cap.
        if expires_at is None:
            expires_at = self.clock.now + 90 * DAY_S
        self.book.add(grant(device_id), expires_at=expires_at)

    def test_an_unused_pairing_is_refused_at_7_days_and_not_one_second_before(self) -> None:
        self.add("unused_early")
        self.add("unused_at")
        self.clock.now += UNUSED_S - 1
        self.assertTrue(self.book.admit("unused_early", "read").ok)  # 7d - 1s, no earlier call
        self.clock.now += 1
        self.assertEqual(self.book.admit("unused_at", "read").code, "device_expired")  # exactly 7d

    def test_a_used_pairing_is_refused_at_30_days_and_not_one_second_before(self) -> None:
        self.add("capped")
        # Accepted calls keep the unused window open, so only the 30-day cap can refuse it.
        for step in (0, 6 * DAY_S, 12 * DAY_S, 18 * DAY_S, 24 * DAY_S):
            self.clock.now = 1000.0 + step
            self.assertTrue(self.book.admit("capped", "read").ok, step)
        self.clock.now = 1000.0 + MAX_AGE_S - 1
        self.assertTrue(self.book.admit("capped", "read").ok)  # 30d - 1s
        self.clock.now = 1000.0 + MAX_AGE_S
        self.assertEqual(self.book.admit("capped", "read").code, "device_expired")  # exactly 30d

    def test_an_accepted_call_restarts_the_unused_bound_and_a_refusal_does_not(self) -> None:
        self.add("used_early")
        self.add("used_at")
        self.add("refused")
        self.clock.now += 6 * DAY_S
        self.assertTrue(self.book.admit("used_early", "read").ok)
        self.assertTrue(self.book.admit("used_at", "read").ok)
        self.assertEqual(self.book.admit("refused", "manage").code, "scope_refused")
        self.clock.now += UNUSED_S - 1  # 7d - 1s after that accept; day 7 from pairing is already past
        self.assertTrue(self.book.admit("used_early", "read").ok)
        self.assertEqual(self.book.admit("refused", "read").code, "device_expired")
        self.clock.now += 1  # exactly 7d after used_at's only accepted call
        self.assertEqual(self.book.admit("used_at", "read").code, "device_expired")

    def test_a_second_accepted_call_restarts_the_unused_bound_again(self) -> None:
        self.add("again")
        self.clock.now += 1 * DAY_S
        self.assertTrue(self.book.admit("again", "read").ok)
        self.clock.now += 5 * DAY_S  # day 6, inside the first window
        self.assertTrue(self.book.connect("again", "conn-1").ok)  # an accepted connect, scope read
        self.clock.now += UNUSED_S - 1  # 13d - 1s, measured from the connect
        self.assertTrue(self.book.check_connection("conn-1", "read").ok)
        self.clock.now += UNUSED_S
        self.assertEqual(self.book.check_connection("conn-1", "read").code, "device_expired")

    def test_a_requested_expiry_beyond_30_days_cannot_outlive_the_cap(self) -> None:
        record = self.book.add(grant("long"), expires_at=self.clock.now + 90 * DAY_S)
        self.assertLessEqual(record.expires_at - self.clock.now, MAX_AGE_S)
        self.assertEqual(record.expires_at - self.clock.now, MAX_AGE_S)

    def test_a_shorter_caller_expiry_is_still_honoured(self) -> None:
        self.add("short", expires_at=self.clock.now + 3600)
        self.clock.now += 3599
        self.assertTrue(self.book.admit("short", "read").ok)
        self.clock.now += 1
        self.assertEqual(self.book.admit("short", "read").code, "device_expired")


class AssertionTests(unittest.TestCase):
    def gate(self, clock: support.FakeClock):
        gate_cls = getattr(pairing, "AssertionBook", None)
        self.assertIsNotNone(gate_cls, "a dangerous action has no assertion gate")
        return gate_cls(clock=clock)

    def test_an_assertion_is_accepted_at_59_seconds_and_refused_at_60_and_later(self) -> None:
        clock = support.FakeClock()
        gate = self.gate(clock)
        early, at_limit, later = gate.issue("run"), gate.issue("approve"), gate.issue("run")
        clock.now += ASSERTION_S - 1
        gate.accept(early, "run")
        clock.now += 1
        with self.assertRaises(PairingRefused) as caught:
            gate.accept(at_limit, "approve")
        self.assertEqual(caught.exception.code, "assertion_expired")
        clock.now += 1
        with self.assertRaises(PairingRefused) as caught:
            gate.accept(later, "run")
        self.assertEqual(caught.exception.code, "assertion_expired")

    def test_replaying_one_assertion_for_a_second_action_is_refused(self) -> None:
        clock = support.FakeClock()
        gate = self.gate(clock)
        issued = gate.issue("run")
        clock.now += ASSERTION_S - 1
        gate.accept(issued, "run")
        with self.assertRaises(PairingRefused) as caught:
            gate.accept(issued, "approve")
        self.assertEqual(caught.exception.code, "assertion_replayed")
        fresh = gate.issue("approve")
        gate.accept(fresh, "approve")  # a different assertion, still inside the window

    def test_the_same_assertion_cannot_be_replayed_for_its_own_action(self) -> None:
        clock = support.FakeClock()
        gate = self.gate(clock)
        issued = gate.issue("approve")
        gate.accept(issued, "approve")
        with self.assertRaises(PairingRefused) as caught:
            gate.accept(issued, "approve")
        self.assertEqual(caught.exception.code, "assertion_replayed")

    def test_a_passkeyless_device_still_cannot_run_or_approve(self) -> None:
        clock = support.FakeClock()
        gate = self.gate(clock)
        book = DeviceBook(clock=clock)
        book.add(grant("dev", scopes=("read", "stop")), expires_at=clock.now + MAX_AGE_S)
        issued = gate.issue("run")
        gate.accept(issued, "run")  # the assertion is fresh; it does not grant the scope
        self.assertEqual(book.admit("dev", "run").code, "scope_refused")
        self.assertEqual(book.admit("dev", "approve").code, "scope_refused")
        self.assertTrue(book.admit("dev", "read").ok)
        self.assertTrue(book.admit("dev", "stop").ok)

    def test_watch_and_stop_are_not_dangerous_and_take_no_assertion(self) -> None:
        gate = self.gate(support.FakeClock())
        for action in ("read", "stop", "manage", ""):
            with self.subTest(action=action):
                with self.assertRaises(PairingRefused) as caught:
                    gate.issue(action)
                self.assertEqual(caught.exception.code, "action_not_dangerous")

    def test_a_second_caller_cannot_consume_the_same_assertion(self) -> None:
        clock = HookClock()
        gate = self.gate(clock)
        issued = gate.issue("run")
        done, outcome = threading.Event(), []

        def second() -> None:
            try:
                gate.accept(issued, "run")
            except PairingRefused as exc:
                outcome.append(exc.code)
            else:
                outcome.append("accepted")
            finally:
                done.set()

        worker = threading.Thread(target=second)

        def in_the_window() -> None:
            worker.start()
            self.assertFalse(done.wait(0.2), "a second accept finished inside the first")

        clock.armed = in_the_window
        gate.accept(issued, "run")
        worker.join(support.WATCHDOG_S)
        self.assertEqual(outcome, ["assertion_replayed"])

    def test_a_wrong_assertion_is_a_typed_refusal(self) -> None:
        gate = self.gate(support.FakeClock())
        issued = gate.issue("run")
        for bad in (None, 7, "", "missing"):
            with self.subTest(bad=bad), self.assertRaises(PairingRefused) as caught:
                gate.accept(bad, "run")
            self.assertEqual(caught.exception.code, "assertion_invalid")
        with self.assertRaises(PairingRefused) as caught:
            gate.accept(issued, "approve")
        self.assertEqual(caught.exception.code, "assertion_wrong_action")
        gate.accept(issued, "run")


if __name__ == "__main__":
    unittest.main()
