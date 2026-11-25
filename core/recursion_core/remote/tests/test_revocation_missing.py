"""A missing record of revocations (lane REMOTE): a revoked phone never comes back by a deleted file.

Each test makes its own temporary state folder; a "restart" is a new ``DeviceBook`` on a new store of the same
folder. Clocks are moved by hand (``support.FakeClock``).
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest

from recursion_core.remote import revocation_store
from recursion_core.remote.device_state import REVOKED_CONNECTION_HELD, DeviceBook
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
        self.mark = os.path.join(os.path.dirname(self.path), ".revocations.json.recorded")

    def book(self) -> DeviceBook:
        return DeviceBook(clock=self.clock, wall=self.wall, store=FileRevocationStore(self.state))

    def paired(self, book: DeviceBook, device_id: str) -> None:
        book.add(grant(device_id), expires_at=self.clock.now + 3600)

    def revoked_a(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        first.revoke("dev_a")

    def forget_command(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = revocation_store.main(["--state-dir", self.state, revocation_store.COMMAND_FORGET])
        return code, out.getvalue(), err.getvalue()


class MissingRecordTests(Folder):
    def test_a_deleted_record_refuses_pairing_and_connect_and_says_what_to_do(self) -> None:
        self.revoked_a()
        os.unlink(self.path)
        after = self.book()
        with self.assertRaises(PairingRefused) as caught:
            self.paired(after, "dev_a")
        self.assertEqual(caught.exception.code, "revocations_unreadable")
        detail = caught.exception.detail
        for words in ("could not be read", "no phone is let in until it can be", self.path, "is missing",
                      "revocations were recorded", self.mark, "Put the file back from a copy", "stop the daemon",
                      f"--state-dir {self.state} {revocation_store.COMMAND_FORGET}",
                      "every phone revoked before may pair again", "remove every paired phone",
                      "pair each one anew"):
            self.assertIn(words, detail)
        decision = after.connect("dev_b", "c")
        self.assertEqual((decision.ok, decision.code, decision.detail), (False, "revocations_unreadable", detail))
        status = after.status()
        self.assertEqual((status["revocations_readable"], status["revocations_unreadable"]), (False, detail))
        self.assertFalse(os.path.exists(self.path), "a refusing start writes no record")

    def test_a_record_put_back_holds_again(self) -> None:
        self.revoked_a()
        good = open(self.path, "rb").read()
        os.unlink(self.path)
        after = self.book()
        self.assertEqual(after.connect("dev_b", "c").code, "revocations_unreadable")
        with open(self.path, "wb") as handle:
            handle.write(good)
        os.chmod(self.path, 0o600)
        with self.assertRaises(PairingRefused) as caught:
            self.paired(after, "dev_a")
        self.assertEqual(caught.exception.code, "device_exists")
        self.paired(after, "dev_b")
        self.assertEqual(after.connect("dev_b", "c").detail, REVOKED_CONNECTION_HELD)

    def test_the_command_starts_a_new_record_knowingly(self) -> None:
        self.revoked_a()
        os.unlink(self.path)
        code, out, err = self.forget_command()
        self.assertEqual((code, err), (0, ""))
        for words in (self.path, "now empty", "every phone revoked before may pair again", "pair each one anew"):
            self.assertIn(words, out)
        self.assertFalse(os.path.lexists(self.mark))
        after = self.book()
        self.paired(after, "dev_a")
        self.assertTrue(after.connect("dev_a", "c").ok)

    def test_the_command_empties_a_record_that_holds_revocations(self) -> None:
        self.revoked_a()
        self.assertEqual(self.forget_command()[0], 0)
        self.assertEqual(FileRevocationStore(self.state).load().devices, {})
        after = self.book()
        self.paired(after, "dev_a")

    def test_a_revoke_on_a_deleted_record_says_it_was_not_written(self) -> None:
        book = self.book()
        self.paired(book, "dev_a")
        self.paired(book, "dev_b")
        book.revoke("dev_a")
        os.unlink(self.path)
        with self.assertRaises(PairingRefused) as caught:
            book.revoke("dev_b")
        self.assertEqual(caught.exception.code, "revocation_unwritten")
        self.assertIn("is missing", caught.exception.detail)
        self.assertFalse(os.path.exists(self.path))


class MarkTests(Folder):
    def test_a_fresh_folder_that_never_held_a_revocation_works_as_before(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        self.assertTrue(first.connect("dev_a", "c").ok)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.lexists(self.mark))
        os.unlink(self.path)                       # no revocation was ever written: nothing is lost
        after = self.book()
        self.paired(after, "dev_a")
        self.assertTrue(after.connect("dev_a", "c").ok)
        self.assertTrue(after.status()["revocations_readable"])

    def test_the_first_revocation_writes_the_mark_owner_only_and_all_or_nothing(self) -> None:
        self.revoked_a()
        self.assertEqual(revocation_store.REVOCATIONS_MARK, os.path.basename(self.mark))
        self.assertEqual(stat.S_IMODE(os.lstat(self.mark).st_mode), 0o600)
        self.assertEqual(open(self.mark, "rb").read(), revocation_store.MARK_TEXT)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n.endswith(".tmp")], [])

    def test_a_folder_with_revocations_but_no_mark_gets_it_at_the_next_start(self) -> None:
        self.revoked_a()
        os.unlink(self.mark)                       # a folder written before the mark existed
        self.book()
        self.assertTrue(os.path.exists(self.mark))
        os.unlink(self.path)
        with self.assertRaises(PairingRefused) as caught:
            self.paired(self.book(), "dev_a")
        self.assertEqual(caught.exception.code, "revocations_unreadable")

    def test_every_unreadable_refusal_says_deleting_the_file_lets_no_phone_in(self) -> None:
        self.revoked_a()
        with open(self.path, "wb") as handle:
            handle.write(b"{not json")
        decision = self.book().connect("dev_b", "c")
        self.assertEqual(decision.code, "revocations_unreadable")
        self.assertIn("deleting it lets no phone in", decision.detail)


if __name__ == "__main__":
    unittest.main()
