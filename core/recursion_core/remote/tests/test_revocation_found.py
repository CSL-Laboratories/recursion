"""What stands in the record's place, and a revoke of an id no phone can have (lane REMOTE): the words say what was
found and what was done.

Each test makes its own temporary state folder.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import FileRevocationStore
from recursion_core.remote.tests import support

PAIR = chr(0xD83D) + chr(0xDE00)   # two lone surrogates in a row: read back as the one character they form


class InThePlaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.path = FileRevocationStore(self.state).path

    def refusal(self) -> str:
        decision = DeviceBook(clock=support.FakeClock(), store=FileRevocationStore(self.state)).connect("dev_a", "c")
        self.assertEqual(decision.code, "revocations_unreadable")
        return decision.detail

    def test_a_link_in_the_place_of_the_record(self) -> None:
        real = os.path.join(self._tmp.name, "real.json")
        with open(real, "wb") as handle:
            handle.write(b"{}")
        os.symlink(real, self.path)
        self.assertIn("it is a link: put the real file in its place", self.refusal())

    def test_a_dangling_link_in_the_place_of_the_record(self) -> None:
        os.symlink(os.path.join(self._tmp.name, "nowhere"), self.path)
        self.assertIn("it is a link: put the real file in its place", self.refusal())

    def test_a_folder_in_the_place_of_the_record(self) -> None:
        os.mkdir(self.path, 0o700)
        self.assertIn("it is a folder, not a file", self.refusal())

    def test_a_pipe_in_the_place_of_the_record(self) -> None:
        os.mkfifo(self.path, 0o600)
        self.assertIn("it is a pipe, not a file", self.refusal())


class RevokeOfAnIdNoPhoneHasTests(unittest.TestCase):
    def test_a_revoke_of_an_id_the_record_refuses_says_nothing_was_revoked(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        book = DeviceBook(clock=support.FakeClock(), store=FileRevocationStore(os.path.join(tmp.name, "state")))
        book.add(PairingGrant("off1", "dev_a", "phone", ("read",), 0.0), expires_at=5000.0)
        with self.assertRaises(PairingRefused) as caught:
            book.revoke(PAIR)
        self.assertEqual(caught.exception.code, "device_unknown")
        for words in ("no phone has this id", "none can be paired with it", "nothing was revoked"):
            self.assertIn(words, caught.exception.detail)
        self.assertEqual(book.status()["revocations_unwritten"], [])
        self.assertTrue(book.admit("dev_a", "read").ok)


if __name__ == "__main__":
    unittest.main()
