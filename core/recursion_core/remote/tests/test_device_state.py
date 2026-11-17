"""A paired device's lifecycle (lane REMOTE): scopes without manage, expiry, revocation that closes connections.

The clock is moved by hand (``support.FakeClock``); nothing waits on the wall clock.
"""

from __future__ import annotations

import dataclasses
import threading
import unittest

from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.tests import support


def grant(device_id: str = "dev_one", scopes=("read", "stop"), label: str = "phone") -> PairingGrant:
    return PairingGrant("off1", device_id, label, tuple(scopes), 0.0)


class WindowClock(support.FakeClock):
    """A clock that runs ``armed`` once, on its next reading, from inside the book's lock."""

    armed = None

    def __call__(self) -> float:
        hook, self.armed = self.armed, None
        if hook is not None:
            hook()
        return self.now


class DeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = support.FakeClock()
        self.book = DeviceBook(clock=self.clock)
        self.book.add(grant(), expires_at=self.clock.now + 3600)

    def test_a_device_gets_its_granted_scopes_and_never_manage(self) -> None:
        self.assertTrue(self.book.admit("dev_one", "read").ok)
        self.assertTrue(self.book.admit("dev_one", "stop").ok)
        for scope in ("manage", "run", "approve", "anything"):
            with self.subTest(scope=scope):
                decision = self.book.admit("dev_one", scope)
                self.assertEqual((decision.ok, decision.code), (False, "scope_refused"))
        self.assertEqual(self.book.admit("dev_one", "manage").detail, "pairing never grants manage")

    def test_a_grant_that_names_manage_is_refused(self) -> None:
        with self.assertRaises(PairingRefused) as caught:
            self.book.add(grant("dev_two", scopes=("read", "manage")), expires_at=self.clock.now + 60)
        self.assertEqual(caught.exception.code, "scope_refused")

    def test_a_device_is_refused_at_its_expiry(self) -> None:
        self.clock.now += 3600 - 0.5  # binary fractions: the sums are exact
        self.assertTrue(self.book.admit("dev_one", "read").ok)
        self.clock.now += 0.5
        self.assertEqual(self.book.admit("dev_one", "read").code, "device_expired")

    def test_a_revoked_device_is_refused_next_and_its_connections_are_named(self) -> None:
        self.assertTrue(self.book.connect("dev_one", "conn-a").ok)
        self.assertTrue(self.book.connect("dev_one", "conn-b", scope="stop").ok)
        self.assertEqual(set(self.book.revoke("dev_one")), {"conn-a", "conn-b"})
        self.assertEqual(self.book.admit("dev_one", "read").code, "device_revoked")
        self.assertEqual(self.book.connect("dev_one", "conn-c").code, "device_revoked")
        self.assertEqual(self.book.revoke("dev_one"), ())

    def test_a_connection_left_open_is_refused_on_its_next_request(self) -> None:
        self.assertTrue(self.book.connect("dev_one", "conn-a").ok)
        self.assertTrue(self.book.check_connection("conn-a", "read").ok)
        self.book.revoke("dev_one")
        self.assertFalse(self.book.check_connection("conn-a", "read").ok)

    def test_a_connection_is_checked_against_the_device_as_it_is_now(self) -> None:
        self.assertTrue(self.book.connect("dev_one", "conn-a").ok)
        self.clock.now += 3600
        self.assertEqual(self.book.check_connection("conn-a", "read").code, "device_expired")
        self.assertEqual(self.book.check_connection("conn-a", "manage").code, "device_expired")

    def test_a_device_id_is_never_reused(self) -> None:
        self.book.revoke("dev_one")
        with self.assertRaises(PairingRefused) as caught:
            self.book.add(grant(), expires_at=self.clock.now + 60)
        self.assertEqual(caught.exception.code, "device_exists")

    def test_an_expiry_in_the_past_is_refused(self) -> None:
        with self.assertRaises(PairingRefused):
            self.book.add(grant("dev_two"), expires_at=self.clock.now)

    def test_a_returned_record_cannot_widen_scopes_or_undo_a_revocation(self) -> None:
        record = self.book.add(grant("dev_two", scopes=("read",)), expires_at=self.clock.now + 60)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.scopes = ("read", "run", "approve")  # type: ignore[misc]
        self.assertEqual(self.book.admit("dev_two", "run").code, "scope_refused")
        self.book.revoke("dev_two")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.revoked_at = None  # type: ignore[misc]
        self.assertEqual(self.book.admit("dev_two", "read").code, "device_revoked")
        self.assertIsNone(record.revoked_at)  # the snapshot handed out earlier; the book's own record changed

    def test_a_connection_id_is_admitted_once(self) -> None:
        self.assertTrue(self.book.connect("dev_one", "conn-a").ok)
        self.assertEqual(self.book.connect("dev_one", "conn-a").code, "connection_exists")

    def test_wrong_arguments_get_typed_refusals(self) -> None:
        for bad_grant in (None, {"device_id": "dev_x"}):
            with self.subTest(grant=bad_grant), self.assertRaises(PairingRefused) as caught:
                self.book.add(bad_grant, expires_at=self.clock.now + 60)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "grant_invalid")
        for expiry in (None, "soon", True, float("inf"), float("nan")):
            with self.subTest(expiry=expiry), self.assertRaises(PairingRefused) as caught:
                self.book.add(grant("dev_two"), expires_at=expiry)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "expiry_invalid")
        self.assertEqual(self.book.connect("dev_one", []).code, "connection_invalid")  # type: ignore[arg-type]
        self.assertEqual(self.book.check_connection([], "read").code, "connection_invalid")  # type: ignore[arg-type]
        self.book.disconnect([])  # type: ignore[arg-type]

    def test_a_grant_whose_device_id_is_not_a_string_is_refused(self) -> None:
        for device_id in ([], {}, 7, None, ""):
            with self.subTest(device_id=device_id), self.assertRaises(PairingRefused) as caught:
                self.book.add(grant(device_id), expires_at=self.clock.now + 60)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "grant_invalid")
        self.assertEqual([row["device_id"] for row in self.book.list()], ["dev_one"])

    def test_a_device_id_that_cannot_be_a_key_is_a_typed_refusal_not_a_type_error(self) -> None:
        class UnhashableStr(str):
            __hash__ = None  # type: ignore[assignment]

        class ExplodingHashStr(str):
            def __hash__(self) -> int:
                raise TypeError("cannot hash this")

        class UnhashableBytes(bytes):
            __hash__ = None  # type: ignore[assignment]

        class UnhashableInt(int):
            __hash__ = None  # type: ignore[assignment]

        for device_id in (UnhashableStr("dev_x"), ExplodingHashStr("dev_x"), UnhashableBytes(b"dev_x"), UnhashableInt(7)):
            kind = type(device_id).__name__
            with self.subTest(add=kind), self.assertRaises(PairingRefused) as caught:
                self.book.add(grant(device_id), expires_at=self.clock.now + 60)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "grant_invalid")
            with self.subTest(revoke=kind), self.assertRaises(PairingRefused) as caught:
                self.book.revoke(device_id)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "device_unknown")
            with self.subTest(admit=kind):
                self.assertEqual(self.book.admit(device_id, "read").code, "device_unknown")
            with self.subTest(connect=kind):
                self.assertEqual(self.book.connect(device_id, "conn_x").code, "device_unknown")
        self.assertEqual([row["device_id"] for row in self.book.list()], ["dev_one"])

    def test_a_plain_str_subclass_is_not_the_device_id_it_spells(self) -> None:
        class PlainSub(str):
            pass

        self.assertTrue(self.book.connect("dev_one", "keep").ok)
        look = PlainSub("dev_one")
        with self.assertRaises(PairingRefused) as caught:
            self.book.add(grant(look), expires_at=self.clock.now + 60)
        self.assertEqual(caught.exception.code, "grant_invalid")
        with self.assertRaises(PairingRefused) as caught:
            self.book.revoke(look)
        self.assertEqual(caught.exception.code, "device_unknown")
        self.assertEqual(self.book.admit(look, "read").code, "device_unknown")
        self.assertEqual(self.book.connect(look, "conn_look").code, "device_unknown")
        self.assertTrue(self.book.admit("dev_one", "read").ok)
        self.assertTrue(self.book.check_connection("keep", "read").ok)
        novel = PlainSub("dev_novel")
        with self.assertRaises(PairingRefused) as caught:
            self.book.add(grant(novel), expires_at=self.clock.now + 60)
        self.assertEqual(caught.exception.code, "grant_invalid")
        self.assertEqual([row["device_id"] for row in self.book.list()], ["dev_one"])
        self.assertTrue(all(type(row["device_id"]) is str for row in self.book.list()))

    def test_the_list_shows_state_and_no_secret(self) -> None:
        self.book.add(grant("dev_two", scopes=("read",), label="tablet"), expires_at=self.clock.now + 10)
        self.book.revoke("dev_one")
        self.clock.now += 10
        rows = {row["device_id"]: row for row in self.book.list()}
        self.assertEqual(rows["dev_one"]["state"], "revoked")
        self.assertEqual(rows["dev_two"]["state"], "expired")
        self.assertEqual(set(rows["dev_two"]), {"device_id", "label", "scopes", "paired_at", "expires_at", "state"})

    def test_a_revocation_in_the_middle_of_a_connect_waits_and_then_names_it(self) -> None:
        clock = WindowClock()
        book = DeviceBook(clock=clock)
        book.add(grant(), expires_at=clock.now + 3600)
        named, done = [], threading.Event()
        worker = threading.Thread(target=lambda: (named.extend(book.revoke("dev_one")), done.set()))

        def in_the_window() -> None:  # after connect's revocation check, before it records the connection
            worker.start()
            self.assertFalse(done.wait(0.2), "a revocation finished inside a connect")  # it has not happened yet

        clock.armed = in_the_window
        decision = book.connect("dev_one", "conn-x")
        worker.join(support.WATCHDOG_S)
        self.assertTrue(decision.ok)
        self.assertEqual(named, ["conn-x"])

    def test_racing_connects_are_either_refused_or_named_by_the_revocation(self) -> None:
        workers = 32
        barrier = threading.Barrier(workers + 1)
        admitted, lock = [], threading.Lock()

        def connect(n: int) -> None:
            barrier.wait(support.WATCHDOG_S)
            decision = self.book.connect("dev_one", f"conn-{n}")
            if decision.ok:
                with lock:
                    admitted.append(f"conn-{n}")
            else:
                self.assertEqual(decision.code, "device_revoked")

        threads = [threading.Thread(target=connect, args=(n,)) for n in range(workers)]
        for thread in threads:
            thread.start()
        barrier.wait(support.WATCHDOG_S)
        closing = set(self.book.revoke("dev_one"))
        for thread in threads:
            thread.join(support.WATCHDOG_S)
        self.assertLessEqual(set(admitted), closing)
        self.assertEqual(self.book.connect("dev_one", "late").code, "device_revoked")


if __name__ == "__main__":
    unittest.main()
