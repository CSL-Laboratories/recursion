"""The whole relay folder deleted (lane REMOTE): why a revoked phone still cannot come back today.

Pairings live in memory and every device id is drawn at random when an offer is redeemed, so a book that starts on
an empty folder knows no device, and a phone revoked before cannot get its id back. The day pairings are kept on
disk, this test is the one that goes red if deleting the folder would let such a phone in.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import OfferBook
from recursion_core.remote.revocation_store import FileRevocationStore
from recursion_core.remote.tests import support


class DeletedFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.clock = support.FakeClock()

    def paired(self, offers: OfferBook, book: DeviceBook) -> str:
        issued = offers.issue(scopes=("read",), label="phone")
        grant = offers.redeem(issued.offer_id, issued.code, source="peer")
        book.add(grant, expires_at=self.clock.now + 3600)
        return grant.device_id

    def check_deleted(self, what: str) -> None:
        book = DeviceBook(clock=self.clock, store=FileRevocationStore(self.state))
        offers = OfferBook(clock=self.clock)
        device_id = self.paired(offers, book)
        self.assertTrue(book.connect(device_id, "c1").ok)
        book.revoke(device_id)
        shutil.rmtree(os.path.dirname(FileRevocationStore(self.state).path) if what == "relay" else self.state)
        after = DeviceBook(clock=self.clock, store=FileRevocationStore(self.state))
        self.assertEqual(after.connect(device_id, "c2").code, "device_unknown")
        self.assertEqual(after.admit(device_id, "read").code, "device_unknown")
        new_offers = OfferBook(clock=self.clock)
        self.assertNotEqual(self.paired(new_offers, after), device_id)

    def test_the_relay_folder_deleted_gives_a_revoked_phone_no_way_back(self) -> None:
        self.check_deleted("relay")

    def test_the_state_folder_deleted_gives_a_revoked_phone_no_way_back(self) -> None:
        self.check_deleted("state")


if __name__ == "__main__":
    unittest.main()
