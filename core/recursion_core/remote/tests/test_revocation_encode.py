"""A revocation is recorded before anything else can fail, and a revoke that could not be recorded says so (lane
REMOTE).

A connection id is proven at connect by writing it with the record's own encoding and reading it back; the
device's revocation is never held back by a connection record that cannot be written; nothing the next start
cannot read is ever written; and every failure of a write is a ``RevocationNotWritten`` whose words say that the
device is NOT revoked on disk. Each test makes its own temporary state folder; a "restart" is a new ``DeviceBook``
on a new store of the same folder. Ids and clocks are made up here (``support.FakeClock``).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.remote import revocation_store
from recursion_core.remote.device_state import REVOKED_CONNECTION_HELD, DeviceBook, RevocationNotWritten
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import FileRevocationStore, Revocations, RevocationStoreError
from recursion_core.remote.tests import support


def grant(device_id: str) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", ("read",), 0.0)


def nested(depth: int) -> object:
    """A str inside ``depth`` tuples."""
    conn: object = "a"
    for _ in range(depth):
        conn = (conn,)
    return conn


class FailingStore:
    """A stand-in store whose writes fail as the test says: ``fail`` is an exception to raise, or None."""

    def __init__(self) -> None:
        self.devices: dict = {}
        self.connections: dict = {}
        self.fail: BaseException | None = None

    def load(self) -> Revocations:
        return Revocations(dict(self.devices), dict(self.connections), None)

    def put(self, change: Revocations) -> None:
        if self.fail is not None:
            raise self.fail
        self.devices.update(change.devices)
        self.connections.update(change.connections)

    def forget(self, conn_ids) -> None:
        if self.fail is not None:
            raise self.fail
        for conn_id in conn_ids:
            self.connections.pop(conn_id, None)


class Stop(BaseException):
    """A failure no ``except Exception`` catches."""


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.clock = support.FakeClock()
        self.wall = support.FakeClock()
        self.wall.now = 1_000_000.0

    def book(self, **kw) -> DeviceBook:
        kw.setdefault("store", FileRevocationStore(self.state))
        return DeviceBook(clock=self.clock, wall=self.wall, **kw)

    def paired(self, book: DeviceBook, device_id: str) -> None:
        book.add(grant(device_id), expires_at=self.clock.now + 3600)

    def assert_refused_on_restart(self, device_id: str) -> None:
        with self.assertRaises(PairingRefused) as caught:
            self.paired(self.book(), device_id)
        self.assertEqual(caught.exception.code, "device_exists")


class RoundTripTests(Folder):
    def test_a_very_long_whole_number_id_is_refused_at_connect_and_the_revocation_takes(self) -> None:
        book = self.book()
        self.paired(book, "d1")
        decision = book.connect("d1", 10 ** 5000)
        self.assertEqual(decision.code, "connection_invalid")
        for words in ("cannot keep this connection id", "640 digits", "reads back unchanged", "new_connection_id"):
            self.assertIn(words, decision.detail)
        self.assertEqual(book.revoke("d1"), ())
        self.assertEqual(book.status()["revocations_unwritten"], [])
        self.assert_refused_on_restart("d1")

    def test_an_id_under_the_interpreter_limit_but_over_the_format_figure_is_refused(self) -> None:
        book = self.book()
        self.paired(book, "d1")
        self.assertEqual(book.connect("d1", 10 ** 699).code, "connection_invalid")      # 700 digits
        self.assertTrue(book.connect("d1", 10 ** 639).ok)                               # 640 digits
        book.revoke("d1")
        after = self.book()
        self.paired(after, "d2")
        self.assertEqual(after.connect("d2", 10 ** 639).detail, REVOKED_CONNECTION_HELD)

    def test_the_round_trip_table(self) -> None:
        self.assertEqual(revocation_store.ID_INT_DIGITS_MAX, 640)
        threshold = getattr(sys.int_info, "str_digits_check_threshold", None)
        if threshold is not None:
            self.assertEqual(revocation_store.ID_INT_DIGITS_MAX, threshold)
        self.assertEqual(revocation_store.ID_DEPTH_MAX, 32)
        kept = ("a", "", "a\x00b", "\ud800", "\udfff x", 0, -1, 10 ** 639, -(10 ** 640 - 1), (), ("a", 1, ("b",)),
                nested(32))
        refused = (10 ** 640, -(10 ** 640), True, False, 1.5, None, b"a", ["a"], frozenset({1}), ("a", 2.0),
                   ("a", True), nested(33), nested(1000))
        for conn in kept:
            with self.subTest(kept=type(conn).__name__):
                self.assertTrue(revocation_store.storable_id(conn))
        for conn in refused:
            with self.subTest(refused=type(conn).__name__):
                self.assertFalse(revocation_store.storable_id(conn))

    def test_a_deep_tuple_is_refused_at_connect_and_never_written(self) -> None:
        book = self.book()
        self.paired(book, "d1")
        for depth in (33, 1000):
            with self.subTest(depth=depth):
                self.assertEqual(book.connect("d1", nested(depth)).code, "connection_invalid")
        self.assertTrue(book.connect("d1", nested(32)).ok)
        book.revoke("d1")
        after = self.book()
        self.paired(after, "d2")
        self.assertEqual(after.connect("d2", nested(32)).detail, REVOKED_CONNECTION_HELD)


class RecordedFirstTests(Folder):
    def test_a_connection_record_that_cannot_be_written_leaves_the_revocation_on_disk(self) -> None:
        book = self.book()
        self.paired(book, "d1")
        self.paired(book, "d2")
        book.connect("d1", "bad")
        book.connect("d1", "good")
        real = revocation_store._encode_id

        def refuses_bad(conn_id, *args):
            if conn_id == "bad":
                raise ValueError("made-up refusal of the encoder")
            return real(conn_id, *args)

        with mock.patch.object(revocation_store, "_encode_id", refuses_bad):
            with self.assertRaises(RevocationNotWritten) as caught:
                book.revoke("d1")
            self.assertEqual(set(caught.exception.connections), {"bad", "good"})
            self.assertTrue(caught.exception.on_disk)
            for words in ("revoked on disk", "1 of its connection records", "after a restart", "Revoke it again"):
                self.assertIn(words, caught.exception.detail)
            status = book.status()
            self.assertEqual(status["revocations_unwritten"], [])
            self.assertEqual(status["revoked_connections_unwritten"], 1)
            self.assertEqual(status["revoked_connections_unwritten_devices"], ["d1"])
            self.assertEqual(book.revoke("d2"), ())      # a record that cannot be written blocks no other device
        held = FileRevocationStore(self.state).load()
        self.assertEqual(set(held.devices), {"d1", "d2"})
        self.assertIn("good", held.connections)
        self.assertNotIn("bad", held.connections)
        self.assertEqual(book.check_connection("bad", "read").code, "device_revoked")
        self.assert_refused_on_restart("d1")
        self.assertEqual(book.revoke("d1"), ())          # the encoder mended: the next revoke writes it
        self.assertIn("bad", FileRevocationStore(self.state).load().connections)
        self.assertEqual(book.status()["revoked_connections_unwritten"], 0)

    def test_every_write_failing_is_a_revocation_not_written_and_the_next_write_carries_it(self) -> None:
        store = FailingStore()
        book = self.book(store=store)
        for device_id in ("d1", "d2"):
            self.paired(book, device_id)
        book.connect("d1", "c")
        for failure in (OSError("made-up disk failure"), ValueError("made-up encoder failure"),
                        RevocationStoreError("made-up refusal")):
            with self.subTest(failure=type(failure).__name__):
                store.fail = failure
                with self.assertRaises(RevocationNotWritten) as caught:
                    book.revoke("d1")
                self.assertFalse(caught.exception.on_disk)
                self.assertIn("made-up", caught.exception.detail)
                self.assertEqual(book.status()["revocations_unwritten"], ["d1"])
                self.assertEqual(book.check_connection("c", "read").code, "device_revoked")
                self.assertEqual(book.connect("d1", "c2").code, "device_revoked")
                self.assertEqual(book.admit("d1", "read").code, "device_revoked")
        store.fail = None
        self.assertEqual(book.revoke("d2"), ())
        self.assertEqual(set(store.devices), {"d1", "d2"})
        self.assertIn("c", store.connections)
        self.assertEqual(book.status()["revocations_unwritten"], [])

    def test_a_failure_no_one_catches_still_leaves_the_device_listed(self) -> None:
        store = FailingStore()
        book = self.book(store=store)
        self.paired(book, "d1")
        store.fail = Stop()
        with self.assertRaises(Stop):
            book.revoke("d1")
        self.assertEqual(book.status()["revocations_unwritten"], ["d1"])
        self.assertEqual(book.admit("d1", "read").code, "device_revoked")
        store.fail = None
        self.assertEqual(book.revoke("d1"), ())
        self.assertIn("d1", store.devices)

    def test_the_words_say_the_device_is_not_revoked_on_disk(self) -> None:
        store = FailingStore()
        book = self.book(store=store)
        self.paired(book, "d1")
        store.fail = RevocationStoreError("the made-up folder is full")
        with self.assertRaises(RevocationNotWritten) as caught:
            book.revoke("d1")
        detail = caught.exception.detail
        for words in ("NOT revoked on disk", "after a restart it would be let in again", "the made-up folder is full",
                      "Revoke it again", "do not restart the daemon"):
            self.assertIn(words, detail)
        self.assertIn("NOT revoked on disk", book.status()["unwritten_words"])


class ReadableAfterTests(Folder):
    def test_a_wall_clock_below_zero_at_a_revoke_writes_nothing_unreadable(self) -> None:
        book = self.book()
        for device_id in ("d0", "d1"):
            self.paired(book, device_id)
        book.revoke("d0")
        self.wall.now = -1.0
        with self.assertRaises(RevocationNotWritten) as caught:
            book.revoke("d1")
        self.assertFalse(caught.exception.on_disk)
        self.assertIn("wall-clock time", caught.exception.detail)
        held = FileRevocationStore(self.state).load()
        self.assertEqual(set(held.devices), {"d0"})
        after = self.book()
        self.paired(after, "d2")                         # the record is readable: a new phone pairs
        self.assert_refused_on_restart("d0")
        self.wall.now = 1_000_100.0                      # the clock set right: the next revoke writes it
        self.assertEqual(book.revoke("d1"), ())
        self.assert_refused_on_restart("d1")

    def test_the_bytes_are_read_back_before_they_replace_the_file(self) -> None:
        book = self.book()
        self.paired(book, "d0")
        self.paired(book, "d1")
        book.revoke("d0")
        path = FileRevocationStore(self.state).path
        with open(path, "rb") as handle:
            before = handle.read()
        self.wall.now = float("nan")
        with mock.patch.object(revocation_store, "_writable_device", lambda device_id, at: True):
            with self.assertRaises(RevocationNotWritten) as caught:
                book.revoke("d1")
        self.assertIn("could not be read back at the next start", caught.exception.detail)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_a_device_time_too_large_for_a_float_is_refused_unreadable_and_the_book_starts(self) -> None:
        store = FileRevocationStore(self.state)
        doc = {"version": revocation_store.REVOCATIONS_VERSION, "boot": None, "devices": {"d1": 10 ** 400},
               "connections": []}
        fd = os.open(store.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(doc))
        book = self.book()
        decision = book.connect("d2", "c")
        self.assertEqual(decision.code, "revocations_unreadable")
        self.assertIn("revoked device of an unknown shape", decision.detail)
        with self.assertRaises(PairingRefused) as caught:
            self.paired(book, "d2")
        self.assertEqual(caught.exception.code, "revocations_unreadable")


if __name__ == "__main__":
    unittest.main()
