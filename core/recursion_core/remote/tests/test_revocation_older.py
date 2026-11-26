"""An older copy of the record of revocations (lane REMOTE): an older copy of the record alone, put back while the
count beside it stands, is refused, so it lets no later revoked phone in. A copy of the whole relay folder put back
together, or an older record with the count taken away, is not seen by the count (the module text says so).

Each test makes its own temporary state folder; a "restart" is a new ``DeviceBook`` on a new store of the same
folder. Clocks are moved by hand (``support.FakeClock``).
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest

from recursion_core.remote import revocation_store
from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import FileRevocationStore
from recursion_core.remote.tests import support


def grant(device_id: str) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", ("read",), 0.0)


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.clock = support.FakeClock()
        self.wall = support.FakeClock()
        self.wall.now = 1_000_000.0
        self.path = FileRevocationStore(self.state).path
        self.count = os.path.join(os.path.dirname(self.path), ".revocations.json.count")

    def book(self) -> DeviceBook:
        return DeviceBook(clock=self.clock, wall=self.wall, store=FileRevocationStore(self.state))

    def revoked(self, book: DeviceBook, device_id: str, conn_id: str) -> None:
        book.add(grant(device_id), expires_at=self.clock.now + 3600)
        self.assertTrue(book.connect(device_id, conn_id).ok)
        book.revoke(device_id)

    def put_back(self, data: bytes) -> None:
        with open(self.path, "wb") as handle:
            handle.write(data)
        os.chmod(self.path, 0o600)

    def older_copy_put_back(self) -> None:
        """dev_a revoked, a copy taken, dev_x revoked, the copy written back."""
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        copy = open(self.path, "rb").read()
        self.revoked(book, "dev_x", "x1")
        self.put_back(copy)


class OlderCopyTests(Folder):
    def test_an_older_copy_put_back_is_refused_and_says_what_to_do(self) -> None:
        self.older_copy_put_back()
        after = self.book()
        with self.assertRaises(PairingRefused) as caught:
            after.add(grant("dev_x"), expires_at=self.clock.now + 3600)
        self.assertEqual(caught.exception.code, "revocations_unreadable")
        detail = caught.exception.detail
        for words in (self.path, "OLDER than the last revocation written in it", "1 of the 2 revocations",
                      "is missing", "A phone revoked after that copy would be let in again",
                      "put back the LATEST copy", "to start knowingly with fewer",
                      f"--state-dir {self.state} {revocation_store.COMMAND_FORGET}"):
            self.assertIn(words, detail)
        decision = after.connect("dev_b", "x1")
        self.assertEqual((decision.ok, decision.code, decision.detail), (False, "revocations_unreadable", detail))
        with self.assertRaises(PairingRefused) as caught:
            after.add(grant("dev_new"), expires_at=self.clock.now + 3600)
        self.assertEqual(caught.exception.code, "revocations_unreadable")

    def test_a_revoke_on_an_older_copy_is_not_written_and_the_copy_stays(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        copy = open(self.path, "rb").read()
        book.add(grant("dev_b"), expires_at=self.clock.now + 3600)
        self.revoked(book, "dev_x", "x1")
        self.put_back(copy)
        with self.assertRaises(PairingRefused) as caught:
            book.revoke("dev_b")
        self.assertEqual(caught.exception.code, "revocation_unwritten")
        self.assertIn("OLDER", caught.exception.detail)
        self.assertEqual(open(self.path, "rb").read(), copy)

    def test_the_latest_copy_put_back_holds(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        self.revoked(book, "dev_x", "x1")
        latest = open(self.path, "rb").read()
        os.unlink(self.path)
        self.put_back(latest)
        after = self.book()
        with self.assertRaises(PairingRefused) as caught:
            after.add(grant("dev_x"), expires_at=self.clock.now + 3600)
        self.assertEqual(caught.exception.code, "device_exists")

    def test_the_forget_command_starts_knowingly_with_fewer(self) -> None:
        self.older_copy_put_back()
        self.assertEqual(revocation_store.main(["--state-dir", self.state, revocation_store.COMMAND_FORGET]), 0)
        self.assertFalse(os.path.lexists(self.count))
        after = self.book()
        after.add(grant("dev_x"), expires_at=self.clock.now + 3600)

    def test_the_count_is_written_owner_only_after_each_revocation(self) -> None:
        book = self.book()
        self.assertFalse(os.path.lexists(self.count), "no revocation yet: no count")
        self.revoked(book, "dev_a", "a1")
        self.assertEqual(stat.S_IMODE(os.lstat(self.count).st_mode), 0o600)
        self.assertEqual(open(self.count, "rb").read(), revocation_store.count_text(1))
        self.revoked(book, "dev_x", "x1")
        self.assertEqual(open(self.count, "rb").read(), revocation_store.count_text(2))
        self.assertIn(b"a copy of it that holds fewer is refused", revocation_store.count_text(2))

    def test_a_record_that_holds_more_than_the_count_is_read_and_the_count_catches_up(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        self.revoked(book, "dev_x", "x1")
        with open(self.count, "wb") as handle:     # a stop between the record's write and the count's
            handle.write(revocation_store.count_text(1))
        after = self.book()
        with self.assertRaises(PairingRefused) as caught:
            after.add(grant("dev_x"), expires_at=self.clock.now + 3600)
        self.assertEqual(caught.exception.code, "device_exists")
        self.assertEqual(open(self.count, "rb").read(), revocation_store.count_text(2))

    def test_a_folder_without_the_count_gets_it_at_the_next_start(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        os.unlink(self.count)                      # a folder written before the count existed
        self.book()
        self.assertEqual(open(self.count, "rb").read(), revocation_store.count_text(1))

    def test_a_count_that_cannot_be_read_refuses_every_phone(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        with open(self.count, "wb") as handle:
            handle.write(b"many\n")
        decision = self.book().connect("dev_b", "c")
        self.assertEqual(decision.code, "revocations_unreadable")
        self.assertIn(self.count, decision.detail)

    def test_the_mark_deleted_with_the_file_still_refuses_by_the_count(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        os.unlink(self.path)
        os.unlink(os.path.join(os.path.dirname(self.path), revocation_store.REVOCATIONS_MARK))
        decision = self.book().connect("dev_b", "a1")
        self.assertEqual(decision.code, "revocations_unreadable")
        self.assertIn("OLDER", decision.detail)


class LatestCopyWordsTests(Folder):
    def test_every_unreadable_refusal_says_the_latest_copy_and_why(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        self.put_back(b"{not json")
        detail = self.book().connect("dev_b", "c").detail
        for words in ("LATEST copy", "an older copy would let every phone revoked after it pair again",
                      "deleting it lets no phone in"):
            self.assertIn(words, detail)

    def test_the_missing_refusal_says_the_latest_copy_and_why(self) -> None:
        book = self.book()
        self.revoked(book, "dev_a", "a1")
        os.unlink(self.path)
        detail = self.book().connect("dev_b", "c").detail
        for words in ("Put the file back from a copy, the LATEST one",
                      "an older copy would let every phone revoked after it pair again"):
            self.assertIn(words, detail)


if __name__ == "__main__":
    unittest.main()
