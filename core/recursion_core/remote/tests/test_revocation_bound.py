"""The record of revoked connections has a bound (lane REMOTE): what is forgotten first, the sweep of timed
records, the cap on a device's live connections, and connection ids the book makes that are never reused.

Each test makes its own temporary state folder where it needs one; clocks are moved by hand.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.remote import device_state
from recursion_core.remote.device_state import (CONNECTIONS_MAX, REVOKED_CONNECTION_HELD, REVOKED_CONNECTION_KEEP_MAX,
                                                DeviceBook)
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import FileRevocationStore
from recursion_core.remote.tests import support
from recursion_core.remote.tests.test_revocation_store import MemoryStore


def grant(device_id: str) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", ("read", "run.stop"), 0.0)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = support.FakeClock()
        self.wall = support.FakeClock()
        self.wall.now = 1_000_000.0
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")

    def book(self, **kw) -> DeviceBook:
        return DeviceBook(clock=self.clock, wall=self.wall, **kw)

    def device(self, book: DeviceBook, device_id: str, conns=()) -> None:
        book.add(grant(device_id), expires_at=self.clock.now + 3600)
        for conn in conns:
            self.assertTrue(book.connect(device_id, conn).ok, conn)

    def other_on(self, book: DeviceBook, conn):
        name = f"other_{len(book.list())}"
        self.device(book, name)
        return book.connect(name, conn)


class BoundTests(Base):
    def test_the_shipped_bound_and_cap(self) -> None:
        self.assertEqual((REVOKED_CONNECTION_KEEP_MAX, CONNECTIONS_MAX), (100_000, 64))
        book = DeviceBook()
        self.assertEqual((book.revoked_connection_keep_max, book.connections_max), (100_000, 64))

    def test_the_settings_refuse_what_is_not_a_value(self) -> None:
        for name in ("revoked_connection_keep_max", "connections_max"):
            for bad in (0, -1, True, 1.5, "none", float("inf")):
                with self.subTest(name=name, bad=bad):
                    with self.assertRaises(PairingRefused) as caught:
                        DeviceBook(**{name: bad})
                    self.assertEqual(caught.exception.code, "setting_invalid")
                    self.assertIn(f"remote.device.{name} is none", caught.exception.detail)
            self.assertIsNone(getattr(DeviceBook(**{name: None}), name))

    def test_released_records_are_forgotten_oldest_release_first(self) -> None:
        book = self.book(revoked_connection_keep_max=3)
        self.device(book, "dev_a", ("c1", "c2", "c3", "c4", "c5"))
        book.revoke("dev_a")
        self.assertEqual(book.status()["bound_passed_by"], 2)
        for conn in ("c1", "c2", "c3"):
            book.disconnect(conn)
        self.assertEqual(book.status()["revoked_connections_kept"], 3)
        self.assertEqual(book.status()["bound_passed_by"], 0)
        self.assertTrue(self.other_on(book, "c1").ok)
        self.assertTrue(self.other_on(book, "c2").ok)
        for conn in ("c3", "c4", "c5"):
            self.assertEqual(self.other_on(book, conn).detail, REVOKED_CONNECTION_HELD)

    def test_a_connection_still_open_is_never_forgotten(self) -> None:
        book = self.book(revoked_connection_keep_max=2)
        self.device(book, "dev_a", ("c1", "c2"))
        self.device(book, "dev_b", ("d1", "d2", "d3"))
        book.revoke("dev_a")
        book.revoke("dev_b")
        status = book.status()
        self.assertEqual((status["revoked_connections_kept"], status["revoked_connections_open"],
                          status["bound_passed_by"]), (5, 5, 3))
        self.assertIn("passed by 3", status["bound_words"])
        self.assertIn("never forgotten", status["bound_words"])
        for conn in ("c1", "c2", "d1", "d2", "d3"):
            with self.subTest(conn=conn):
                self.assertEqual(self.other_on(book, conn).detail, REVOKED_CONNECTION_HELD)
                self.assertEqual(book.check_connection(conn, "read").code, "device_revoked")
        book.disconnect("c1")
        self.assertTrue(self.other_on(book, "c1").ok)
        for conn in ("c2", "d1", "d2", "d3"):
            self.assertEqual(self.other_on(book, conn).detail, REVOKED_CONNECTION_HELD)

    def test_the_record_filled_to_its_shipped_bound_keeps_the_newest(self) -> None:
        book = self.book()
        per, devices = CONNECTIONS_MAX, -(-(REVOKED_CONNECTION_KEEP_MAX + 32) // CONNECTIONS_MAX)
        order = []
        for d in range(devices):
            conns = [(d, i) for i in range(per)]
            self.device(book, f"dev_{d}", conns)
            order.extend(conns)
        for d in range(devices):
            book.revoke(f"dev_{d}")
        self.assertEqual(book.status()["bound_passed_by"], len(order) - REVOKED_CONNECTION_KEEP_MAX)
        for conn in order:
            book.disconnect(conn)
        status = book.status()
        self.assertEqual((status["revoked_connections_kept"], status["bound_passed_by"]), (REVOKED_CONNECTION_KEEP_MAX, 0))
        dropped = len(order) - REVOKED_CONNECTION_KEEP_MAX
        self.assertTrue(self.other_on(book, order[dropped - 1]).ok)
        self.assertEqual(self.other_on(book, order[dropped]).detail, REVOKED_CONNECTION_HELD)

    def test_the_store_forgets_what_the_bound_forgets(self) -> None:
        book = self.book(store=FileRevocationStore(self.state), revoked_connection_keep_max=2)
        self.device(book, "dev_a", ("c1", "c2", "c3"))
        book.revoke("dev_a")
        book.disconnect("c1")
        self.assertEqual(sorted(FileRevocationStore(self.state).load().connections), ["c2", "c3"])
        after = self.book(store=FileRevocationStore(self.state), revoked_connection_keep_max=2)
        self.assertTrue(self.other_on(after, "c1").ok)
        self.assertEqual(self.other_on(after, "c2").detail, REVOKED_CONNECTION_HELD)

    def test_a_smaller_bound_at_a_restart_forgets_the_oldest(self) -> None:
        first = self.book(store=FileRevocationStore(self.state))
        self.device(first, "dev_a", ("c1", "c2", "c3"))
        first.revoke("dev_a")
        for conn in ("c1", "c2", "c3"):
            self.wall.now += 1
            first.disconnect(conn)
        after = self.book(store=FileRevocationStore(self.state), revoked_connection_keep_max=1)
        self.assertEqual(sorted(FileRevocationStore(self.state).load().connections), ["c3"])
        self.assertTrue(self.other_on(after, "c2").ok)
        self.assertEqual(self.other_on(after, "c3").detail, REVOKED_CONNECTION_HELD)


class SweepTests(Base):
    def revoked_and_released(self, book: DeviceBook) -> list:
        conns = []
        for d in range(20):
            mine = [f"d{d}c{i}" for i in range(50)]
            self.device(book, f"dev_{d}", mine)
            conns += mine
        for d in range(20):
            book.revoke(f"dev_{d}")
        for conn in conns:
            book.disconnect(conn)
        return conns

    def test_timed_records_are_dropped_at_the_next_revoke_without_a_look(self) -> None:
        book = self.book(revoked_connection_keep_s=30)
        self.revoked_and_released(book)
        self.assertEqual(book.status()["revoked_connections_kept"], 1000)
        self.clock.now += 1000
        self.assertEqual(book.status()["revoked_connections_kept"], 1000)
        self.device(book, "dev_late")
        book.revoke("dev_late")
        self.assertEqual(book.status()["revoked_connections_kept"], 0)

    def test_timed_records_are_dropped_at_the_next_disconnect_and_in_the_store(self) -> None:
        book = self.book(revoked_connection_keep_s=30, store=FileRevocationStore(self.state))
        self.revoked_and_released(book)
        self.assertEqual(len(FileRevocationStore(self.state).load().connections), 1000)
        self.clock.now += 1000
        self.device(book, "dev_late", ("x",))
        book.disconnect("x")
        self.assertEqual(book.status()["revoked_connections_kept"], 0)
        self.assertEqual(FileRevocationStore(self.state).load().connections, {})

    def test_timed_records_are_dropped_at_the_load(self) -> None:
        book = self.book(revoked_connection_keep_s=30, store=FileRevocationStore(self.state))
        self.device(book, "dev_a", ("c",))
        book.revoke("dev_a")
        book.disconnect("c")
        self.wall.now += 31
        after = self.book(revoked_connection_keep_s=30, store=FileRevocationStore(self.state))
        self.assertEqual(after.status()["revoked_connections_kept"], 0)
        self.assertEqual(FileRevocationStore(self.state).load().connections, {})

    def test_a_connection_open_at_the_stop_is_kept_from_the_restart_on_the_book_clock(self) -> None:
        first = self.book(revoked_connection_keep_s=30, store=FileRevocationStore(self.state))
        self.device(first, "dev_a", ("c",))
        first.revoke("dev_a")
        self.wall.now += 1000
        after = self.book(revoked_connection_keep_s=30, store=FileRevocationStore(self.state))
        self.clock.now += 29.5
        self.assertEqual(self.other_on(after, "c").detail, REVOKED_CONNECTION_HELD)
        self.clock.now += 0.5
        self.assertTrue(self.other_on(after, "c").ok)


class CapTests(Base):
    def test_a_device_past_its_cap_is_refused_with_the_count(self) -> None:
        book = self.book(connections_max=3)
        self.device(book, "dev_a", ("c1", "c2", "c3"))
        decision = book.connect("dev_a", "c4")
        self.assertEqual((decision.ok, decision.code), (False, "connections_full"))
        self.assertIn("already holds 3 open connections, the most allowed", decision.detail)
        self.assertIn("remote.device.connections_max", decision.detail)
        book.disconnect("c1")
        self.assertTrue(book.connect("dev_a", "c4").ok)
        self.device(book, "dev_b", ("d1", "d2", "d3"))

    def test_the_shipped_cap_and_no_cap(self) -> None:
        book = self.book()
        self.device(book, "dev_a", range(CONNECTIONS_MAX))
        self.assertEqual(book.connect("dev_a", CONNECTIONS_MAX).code, "connections_full")
        self.assertEqual(book.revoke("dev_a"), tuple(range(CONNECTIONS_MAX)))
        open_ended = self.book(connections_max=None)
        self.device(open_ended, "dev_b", range(500))


class NeverReusedIdTests(Base):
    def test_ids_the_book_makes_are_never_made_twice(self) -> None:
        book = self.book()
        ids = {book.new_connection_id() for _ in range(5000)}
        self.assertEqual(len(ids), 5000)

    def test_after_a_restart_no_id_the_book_makes_meets_a_held_one(self) -> None:
        first = self.book(store=FileRevocationStore(self.state))
        self.device(first, "dev_a")
        held = [first.new_connection_id() for _ in range(5)]
        for conn in held:
            self.assertTrue(first.connect("dev_a", conn).ok)
        first.revoke("dev_a")
        after = self.book(store=FileRevocationStore(self.state))
        boot = FileRevocationStore(self.state).load().boot
        made = [after.new_connection_id() for _ in range(1000)]
        self.assertTrue(all(conn.startswith(boot + ".") for conn in made))
        self.assertFalse(set(made) & set(held))
        self.device(after, "dev_b")
        self.assertTrue(after.connect("dev_b", made[0]).ok)

    def test_a_boot_that_a_held_id_begins_with_is_drawn_again(self) -> None:
        store = MemoryStore()
        first = DeviceBook(clock=self.clock, wall=self.wall, store=store)
        self.device(first, "dev_a", ("aaaa.1",))
        first.revoke("dev_a")
        draws = iter(["aaaa", "bbbb"])
        with mock.patch.object(device_state.secrets, "token_hex", lambda n: next(draws)):
            after = DeviceBook(clock=self.clock, wall=self.wall, store=store)
        self.assertEqual(after.new_connection_id(), "bbbb.1")
        self.assertEqual(store.boot, "bbbb")

    def test_an_id_a_door_brought_is_skipped(self) -> None:
        book = self.book()
        self.device(book, "dev_a")
        first = book.new_connection_id()
        boot = first.rsplit(".", 1)[0]
        self.assertTrue(book.connect("dev_a", f"{boot}.2").ok)
        self.assertEqual(book.new_connection_id(), f"{boot}.3")


class RaceTests(Base):
    def test_a_revoke_racing_connects_admits_no_other_device_and_survives_a_restart(self) -> None:
        for round_ in range(100):
            store = MemoryStore()
            book = DeviceBook(store=store)
            book.add(grant("dev_a"), expires_at=book.clock() + 3600)
            book.add(grant("dev_b"), expires_at=book.clock() + 3600)
            ids = [book.new_connection_id() for _ in range(4)]
            for conn in ids:
                book.connect("dev_a", conn)
            barrier, admitted = threading.Barrier(2), []

            def revoker():
                barrier.wait()
                book.revoke("dev_a")

            def other():
                barrier.wait()
                for _ in range(2):
                    for conn in ids:
                        if book.connect("dev_b", conn).ok:
                            admitted.append(conn)

            threads = [threading.Thread(target=revoker), threading.Thread(target=other)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(admitted, [], round_)
            after = DeviceBook(store=store)
            after.add(grant("dev_c"), expires_at=after.clock() + 3600)
            self.assertEqual([after.connect("dev_c", conn).detail for conn in ids], [REVOKED_CONNECTION_HELD] * 4)


if __name__ == "__main__":
    unittest.main()
