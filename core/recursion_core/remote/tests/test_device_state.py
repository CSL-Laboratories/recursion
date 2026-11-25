"""A paired device's lifecycle (lane REMOTE): scopes without manage, expiry, revocation that closes connections.

The clock is moved by hand (``support.FakeClock``); nothing waits on the wall clock.
"""

from __future__ import annotations

import dataclasses
import threading
import unittest

from recursion_core.remote import device_state
from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.tests import support


def grant(device_id: str = "dev_one", scopes=("read", "run.stop"), label: str = "phone") -> PairingGrant:
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
        self.assertTrue(self.book.admit("dev_one", "run.stop").ok)
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
        self.assertTrue(self.book.connect("dev_one", "conn-b", scope="run.stop").ok)
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


# A test figure: rounds of the race between a revocation and another device's connects on the same ids.
RACE_ROUNDS = 200
RACE_IDS = 8


class RevokedConnectionTests(unittest.TestCase):
    """A revoked device's connection stays refused as revoked while the door can still present it."""

    def book(self, **options) -> DeviceBook:
        self.clock = support.FakeClock()
        book = DeviceBook(clock=self.clock, **options)
        book.add(grant(), expires_at=self.clock.now + 3600)
        book.add(grant("dev_two"), expires_at=self.clock.now + 3600)
        return book

    def test_a_revoked_connection_is_refused_as_revoked_and_its_id_admits_no_other_device(self) -> None:
        book = self.book()
        self.assertTrue(book.connect("dev_one", "c").ok)
        self.assertEqual(book.revoke("dev_one"), ("c",))
        for _ in range(2):
            self.assertEqual(book.check_connection("c", "read").code, "device_revoked")
            self.assertEqual(book.check_connection("c", "run.stop").code, "device_revoked")
            decision = book.connect("dev_two", "c")
            self.assertEqual((decision.ok, decision.code), (False, "connection_exists"))
            self.assertEqual(decision.detail, device_state.REVOKED_CONNECTION_HELD)
        self.assertEqual(book.revoke("dev_one"), ())
        self.assertEqual(book.connect("dev_one", "c").code, "device_revoked")
        self.assertTrue(book.connect("dev_two", "c2").ok)

    def test_revoke_names_each_live_connection_once(self) -> None:
        book = self.book()
        for conn in ("a", "b"):
            self.assertTrue(book.connect("dev_one", conn).ok)
        self.assertTrue(book.connect("dev_two", "z").ok)
        self.assertEqual(sorted(book.revoke("dev_one")), ["a", "b"])
        self.assertEqual(book.revoke("dev_one"), ())
        self.assertTrue(book.check_connection("z", "read").ok)

    def test_at_a_keep_of_0_the_disconnect_drops_it_and_the_id_is_admitted_as_a_new_one(self) -> None:
        book = self.book(revoked_connection_keep_s=0)
        self.assertTrue(book.connect("dev_one", "c").ok)
        book.revoke("dev_one")
        self.assertEqual(book.connect("dev_two", "c").code, "connection_exists")
        book.disconnect("c")
        self.assertEqual(book._revoked, {})  # the book holds nothing for it after the disconnect
        self.assertEqual(book.check_connection("c", "read").code, "connection_unknown")
        self.assertTrue(book.connect("dev_two", "c").ok)
        self.assertTrue(book.check_connection("c", "read").ok)
        book.disconnect("c")
        self.assertEqual(book.check_connection("c", "read").code, "connection_unknown")

    def test_the_first_value_never_forgets_a_revoked_connection(self) -> None:
        self.assertIsNone(device_state.REVOKED_CONNECTION_KEEP_S)
        book = self.book()
        self.assertIsNone(book.revoked_connection_keep_s)
        self.assertTrue(book.connect("dev_one", "c").ok)
        book.revoke("dev_one")
        book.disconnect("c")
        self.clock.now += 3000.0  # long after the disconnect, short of the devices' own expiry
        self.assertEqual(book.check_connection("c", "read").code, "device_revoked")
        self.assertEqual(book.connect("dev_two", "c").code, "connection_exists")
        book.disconnect("c")
        self.assertEqual(book.connect("dev_two", "c").code, "connection_exists")

    def test_a_keep_of_some_seconds_ends_on_the_books_clock_after_the_disconnect(self) -> None:
        book = self.book(revoked_connection_keep_s=30)
        self.assertTrue(book.connect("dev_one", "c").ok)
        book.revoke("dev_one")
        self.clock.now += 100  # before the disconnect the record is kept whatever the time
        self.assertEqual(book.connect("dev_two", "c").code, "connection_exists")
        book.disconnect("c")
        self.clock.now += 29.5
        self.assertEqual(book.check_connection("c", "read").code, "device_revoked")
        self.assertEqual(book.connect("dev_two", "c").code, "connection_exists")
        self.clock.now += 0.5
        self.assertTrue(book.connect("dev_two", "c").ok)
        self.assertTrue(book.check_connection("c", "read").ok)

    def test_a_keep_that_is_not_a_value_is_refused(self) -> None:
        for keep in (-1, -0.5, float("nan"), "30", True, [30]):
            with self.subTest(keep=keep), self.assertRaises(PairingRefused) as caught:
                DeviceBook(revoked_connection_keep_s=keep)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, "setting_invalid")
        for keep in (None, 0, 0.5, 30, float("inf")):
            with self.subTest(keep=keep):
                self.assertEqual(DeviceBook(revoked_connection_keep_s=keep).revoked_connection_keep_s, keep)

    def test_a_revocation_racing_another_devices_connects_never_hands_it_a_named_connection(self) -> None:
        for round_ in range(RACE_ROUNDS):
            book = self.book()
            ids = [f"c{round_}-{n}" for n in range(RACE_IDS)]
            for conn in ids:
                self.assertTrue(book.connect("dev_one", conn).ok)
            barrier = threading.Barrier(2)
            named, admitted = [], []

            def revoke() -> None:
                barrier.wait(support.WATCHDOG_S)
                named.extend(book.revoke("dev_one"))

            def connect() -> None:
                barrier.wait(support.WATCHDOG_S)
                for _ in range(3):
                    for conn in ids:
                        if book.connect("dev_two", conn).ok:
                            admitted.append(conn)

            threads = [threading.Thread(target=revoke), threading.Thread(target=connect)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(support.WATCHDOG_S)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(sorted(named), sorted(ids))
            self.assertEqual(admitted, [], f"round {round_}")
            for conn in ids:
                self.assertEqual(book.check_connection(conn, "read").code, "device_revoked")


if __name__ == "__main__":
    unittest.main()
