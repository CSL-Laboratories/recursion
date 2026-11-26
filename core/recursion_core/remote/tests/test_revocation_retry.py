"""A record of revocations that cannot be read (lane REMOTE): it is read again outside the book's lock, one read at a
time, at most once per remote.device.revocations_retry_s, so admit and check_connection never wait on the file.

Each test makes its own temporary state folder or uses a stand-in store; clocks are moved by hand
(``support.FakeClock``). The file's lock is held by the test's own descriptor (a flock is held per open file, so
the same process stands in for another one).
"""

from __future__ import annotations

import fcntl
import math
import os
import tempfile
import threading
import time
import unittest

from recursion_core.remote import device_state
from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import FileRevocationStore, RevocationStoreError, Revocations
from recursion_core.remote.tests import support

LOCK_WAIT_S = 2.0      # the test's own store's lock wait
ANSWER_WITHIN_S = 0.5  # the test's own wait for an answer that must not wait on the file


def grant(device_id: str) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", ("read",), 0.0)


class Unreadable:
    """A stand-in store that cannot be read until ``readable`` is set; it counts its loads."""

    def __init__(self) -> None:
        self.loads = 0
        self.readable = False

    def load(self) -> Revocations:
        self.loads += 1
        if not self.readable:
            raise RevocationStoreError("the stand-in cannot be read")
        return Revocations()

    def put(self, change: Revocations) -> None:
        pass

    def forget(self, conn_ids) -> None:
        pass


class OutsideTheLockTests(unittest.TestCase):
    def test_admit_and_check_connection_answer_while_a_read_waits_on_the_file_lock(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = os.path.join(tmp.name, "state")
        store = FileRevocationStore(state, lock_timeout_s=LOCK_WAIT_S)
        with open(store.path, "wb") as handle:
            handle.write(b"{not json")
        os.chmod(store.path, 0o600)
        book = DeviceBook(clock=support.FakeClock(), store=store)
        self.assertFalse(book.status()["revocations_readable"])
        fd = os.open(os.path.join(os.path.dirname(store.path), ".revocations.json.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        answers: list = []
        reader = threading.Thread(target=lambda: answers.append(book.connect("dev_a", "c1")), daemon=True)
        reader.start()
        time.sleep(ANSWER_WITHIN_S / 2)        # the reader is now waiting on the file's lock
        started = time.monotonic()
        checked = book.check_connection("c1", "read")
        admitted = book.admit("dev_a", "read")
        took = time.monotonic() - started
        self.assertLess(took, ANSWER_WITHIN_S, "admit and check_connection waited on the file's lock")
        self.assertEqual((checked.code, admitted.code), ("connection_unknown", "device_unknown"))
        reader.join(LOCK_WAIT_S * 3)
        self.assertFalse(reader.is_alive())
        self.assertEqual(answers[0].code, "revocations_unreadable")
        self.assertIn("held by another writer", answers[0].detail)


class IntervalTests(unittest.TestCase):
    def book(self, store: Unreadable, every) -> tuple[DeviceBook, support.FakeClock]:
        clock = support.FakeClock()
        return DeviceBook(clock=clock, store=store, revocations_retry_s=every), clock

    def test_the_record_is_read_again_at_most_once_per_interval(self) -> None:
        store = Unreadable()
        book, clock = self.book(store, 5.0)
        self.assertEqual(store.loads, 1)
        self.assertEqual(book.connect("dev_a", "c1").code, "revocations_unreadable")
        clock.now += 4.9
        with self.assertRaises(PairingRefused) as caught:
            book.add(grant("dev_a"), expires_at=clock.now + 3600)
        self.assertEqual(caught.exception.code, "revocations_unreadable")
        self.assertEqual(store.loads, 1)
        clock.now += 0.1
        self.assertEqual(book.connect("dev_a", "c1").code, "revocations_unreadable")
        self.assertEqual(store.loads, 2)
        store.readable = True
        self.assertEqual(book.connect("dev_a", "c1").code, "revocations_unreadable")
        clock.now += 5.0
        book.add(grant("dev_a"), expires_at=clock.now + 3600)
        self.assertEqual(store.loads, 3)
        self.assertTrue(book.status()["revocations_readable"])

    def test_none_reads_again_at_every_call(self) -> None:
        store = Unreadable()
        book, _clock = self.book(store, None)
        for _ in range(3):
            book.connect("dev_a", "c1")
        self.assertEqual(store.loads, 4)

    def test_the_shipped_value_is_none_and_a_book_reads_at_every_call_by_it(self) -> None:
        self.assertIsNone(device_state.REVOCATIONS_RETRY_S)
        store = Unreadable()
        book = DeviceBook(clock=support.FakeClock(), store=store)
        book.connect("dev_a", "c1")
        self.assertEqual(store.loads, 2)

    def test_a_value_that_is_not_a_number_of_seconds_is_refused(self) -> None:
        for bad in (-1, -0.5, math.nan, True, "5"):
            with self.subTest(bad=bad):
                with self.assertRaises(PairingRefused) as caught:
                    DeviceBook(store=Unreadable(), revocations_retry_s=bad)
                self.assertEqual(caught.exception.code, "setting_invalid")
                self.assertIn("remote.device.revocations_retry_s", caught.exception.detail)
        DeviceBook(store=Unreadable(), revocations_retry_s=0)
        DeviceBook(store=Unreadable(), revocations_retry_s=math.inf)


if __name__ == "__main__":
    unittest.main()
