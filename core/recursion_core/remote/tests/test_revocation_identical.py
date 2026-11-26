"""Every key and every value the record of revocations writes comes back from its own encoding identical (lane REMOTE).

A device id made of two lone surrogates that form a pair is written by the JSON encoder as two escapes that its
decoder reads back joined, as one other character: the revocation would be on disk under another id, the revoke
answered as done, and after a restart the same id added again. One function, ``comes_back_identical``, proves a
value comes back equal, of the same types and with the same code points; every door a value enters the book by
asks it, and the write asks it of every row and of the whole record. Each test makes its own temporary state
folder; a "restart" is a new ``DeviceBook`` on a new store of the same folder. Ids and clocks are made up here.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from recursion_core.remote import revocation_store
from recursion_core.remote.device_state import REVOKED_CONNECTION_HELD, DeviceBook, RevocationNotWritten
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import (FileRevocationStore, RevocationPartlyWritten, Revocations,
                                                    RevocationStoreError, StoredConnection)
from recursion_core.remote.tests import support

PAIR = chr(0xD83D) + chr(0xDE00)   # two lone surrogates that form a pair: the encoder writes two escapes
JOINED = "\U0001f600"        # what the decoder reads those two escapes back as
PAIR_2 = chr(0xD83D) + chr(0xDE01)
JOINED_2 = "\U0001f601"
NOT_THE_SAME = "does not come back the same"


def grant(device_id: str) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", ("read",), 0.0)


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self.state = support.scratch_dir(self, "rid-")
        self.clock = support.FakeClock()
        self.wall = support.FakeClock()
        self.wall.now = 1_000_000.0

    def book(self, **kw) -> DeviceBook:
        kw.setdefault("store", FileRevocationStore(self.state))
        return DeviceBook(clock=self.clock, wall=self.wall, **kw)

    def paired(self, book: DeviceBook, device_id: str) -> None:
        book.add(grant(device_id), expires_at=self.clock.now + 3600)

    def refused_code(self, book: DeviceBook, device_id: str) -> str | None:
        try:
            self.paired(book, device_id)
        except PairingRefused as exc:
            return exc.code
        return None

    def write_older_file(self, text: str) -> None:
        """A record as an earlier writer left it: the bytes, the file owner-only, and the mark beside it."""
        store = FileRevocationStore(self.state)
        fd = os.open(store.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("ascii"))
        fd = os.open(os.path.join(os.path.dirname(store.path), revocation_store.REVOCATIONS_MARK),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(revocation_store.MARK_TEXT)


class IdenticalTests(Folder):
    def test_the_one_rule_table(self) -> None:
        identical = ("a", "", "a\x00b", "\ud800", "\udfff x", JOINED, 0, -1, 10 ** 639, -(10 ** 640 - 1), 1.5, -0.0,
                     0.1, None, True, False, [], ["s", "a"], {"a": 1.0}, {"version": 1, "rows": [{"id": ["i", 3]}]})
        not_identical = (PAIR, "x" + PAIR + "y", [PAIR], {PAIR: 1.0}, {"a": PAIR}, 10 ** 640, -(10 ** 640),
                         10 ** 5000, (), ("a",), {1: "a"}, {True: 1}, {"a": ("b",)}, b"a", object(), frozenset(),
                         type("S", (str,), {})("a"), type("I", (int,), {})(1), {type("K", (str,), {})("k"): 1})
        for n, value in enumerate(identical):
            with self.subTest(identical=n, kind=type(value).__name__):
                self.assertTrue(revocation_store.comes_back_identical(value))
        for n, value in enumerate(not_identical):
            with self.subTest(not_identical=n, kind=type(value).__name__):
                self.assertFalse(revocation_store.comes_back_identical(value))

    def test_a_device_id_the_decoder_would_join_is_never_answered_as_done_nor_added_after_a_restart(self) -> None:
        book = self.book()
        code = self.refused_code(book, PAIR)
        if code is None:
            with self.assertRaises(RevocationNotWritten):
                book.revoke(PAIR)
        self.assertIn(self.refused_code(self.book(), PAIR), ("grant_invalid", "device_exists"))
        self.assertIsNone(self.refused_code(self.book(), JOINED))   # nobody revoked the joined id

    def test_such_a_device_id_is_refused_at_add_with_words_that_say_why_and_what_to_do(self) -> None:
        with self.assertRaises(PairingRefused) as caught:
            self.paired(self.book(), PAIR)
        self.assertEqual(caught.exception.code, "grant_invalid")
        for words in (NOT_THE_SAME, "two lone surrogates", "pairing door"):
            self.assertIn(words, caught.exception.detail)
        self.paired(self.book(), JOINED)   # the character itself is an id like any other
        self.paired(self.book(), "\ud800")   # one lone surrogate comes back as it went

    def test_every_other_door_names_why_such_an_id_is_unknown(self) -> None:
        book = self.book()
        self.paired(book, "d1")
        for decision in (book.connect(PAIR, "c"), book.admit(PAIR, "read")):
            self.assertEqual(decision.code, "device_unknown")
            self.assertIn(NOT_THE_SAME, decision.detail)
        with self.assertRaises(PairingRefused) as caught:
            book.revoke(PAIR)
        self.assertEqual(caught.exception.code, "device_unknown")
        self.assertIn(NOT_THE_SAME, caught.exception.detail)
        self.assertEqual(book.connect("dx", "c").code, "device_unknown")   # an ordinary unknown id as before
        self.assertEqual(book.connect("dx", "c").detail, "")

    def test_the_write_leaves_out_and_names_a_device_row_that_does_not_come_back(self) -> None:
        store = FileRevocationStore(self.state)
        change = Revocations({PAIR: 5.0, "d2": 6.0}, {"c": StoredConnection(PAIR, None), "c2": StoredConnection("d2")})
        with self.assertRaises(RevocationPartlyWritten) as caught:
            store.put(change)
        self.assertEqual(caught.exception.devices, frozenset({PAIR}))
        self.assertEqual(caught.exception.connections, frozenset({"c"}))
        self.assertIn(NOT_THE_SAME, str(caught.exception))
        self.assertIn(ascii(PAIR)[1:-1], str(caught.exception))   # shown as escapes, so the words can be printed
        str(caught.exception).encode("utf-8")
        held = FileRevocationStore(self.state).load()
        self.assertEqual(dict(held.devices), {"d2": 6.0})
        self.assertEqual(set(held.connections), {"c2"})

    def test_every_connection_row_is_asked_the_rule_by_the_write_itself(self) -> None:
        # Even if an id got past connect's check, the write asks the rule of the row and leaves it out, named.
        store = FileRevocationStore(self.state)
        with mock.patch.object(revocation_store, "storable_id", return_value=True):
            with self.assertRaises(RevocationPartlyWritten) as caught:
                store.put(Revocations({"d1": 5.0}, {PAIR_2: StoredConnection("d1"), "c": StoredConnection("d1")}))
        self.assertEqual(caught.exception.devices, frozenset())
        self.assertEqual(caught.exception.connections, frozenset({PAIR_2}))
        held = FileRevocationStore(self.state).load()
        self.assertEqual(dict(held.devices), {"d1": 5.0})
        self.assertEqual(set(held.connections), {"c"})

    def test_the_whole_record_is_read_back_identical_before_it_replaces_the_file(self) -> None:
        store = FileRevocationStore(self.state)
        store.put(Revocations({"d0": 1.0}, {}))
        with mock.patch.object(revocation_store, "comes_back_identical", return_value=True):
            with self.assertRaises(RevocationStoreError) as caught:
                store.put(Revocations({PAIR: 5.0}, {}))
        self.assertNotIsInstance(caught.exception, RevocationPartlyWritten)
        self.assertIn("does not read back the same", str(caught.exception))
        self.assertEqual(dict(FileRevocationStore(self.state).load().devices), {"d0": 1.0})

    def test_a_boot_id_that_does_not_come_back_is_refused(self) -> None:
        with self.assertRaises(RevocationStoreError) as caught:
            FileRevocationStore(self.state).put(Revocations({}, {}, PAIR))
        self.assertIn(NOT_THE_SAME, str(caught.exception))
        self.assertEqual(FileRevocationStore(self.state).load().boot, None)


class OlderFileTests(Folder):
    def test_an_older_file_that_holds_a_joined_id_loses_no_revocation(self) -> None:
        # An earlier writer revoked the device PAIR: on disk its id is two escapes, read back as JOINED.
        self.write_older_file(
            '{"boot":null,"connections":[{"device":"\\ud83d\\ude00","id":["s","c1"],"released":null},'
            '{"device":"\\ud83d\\ude00","id":["s","\\ud83d\\ude01"],"released":null}],'
            '"devices":{"\\ud83d\\ude00":999000.0},"version":1}')
        book = self.book()
        self.assertEqual(self.refused_code(book, JOINED), "device_exists")
        self.assertEqual(self.refused_code(book, PAIR), "grant_invalid")
        self.assertEqual(book.admit(JOINED, "read").code, "device_revoked")
        self.paired(book, "dnew")
        self.assertEqual(book.connect("dnew", "c1").detail, REVOKED_CONNECTION_HELD)
        self.assertEqual(book.connect("dnew", JOINED_2).detail, REVOKED_CONNECTION_HELD)
        self.assertEqual(book.connect("dnew", PAIR_2).code, "connection_invalid")
        self.assertEqual(book.revoke("dnew"), ())
        after = self.book()
        self.assertEqual(self.refused_code(after, JOINED), "device_exists")
        self.assertEqual(self.refused_code(after, PAIR), "grant_invalid")
        self.assertEqual(self.refused_code(after, "dnew"), "device_exists")
        self.paired(after, "dlater")
        self.assertEqual(after.connect("dlater", "c1").detail, REVOKED_CONNECTION_HELD)
        self.assertEqual(after.connect("dlater", JOINED_2).detail, REVOKED_CONNECTION_HELD)

    def test_two_rows_an_older_file_holds_for_one_id_keep_the_one_that_holds_longer(self) -> None:
        # Written apart (PAIR_2 and JOINED_2 were two ids to an earlier writer), read back as one id.
        for released in ('null', '999990.0'):
            other = '999990.0' if released == 'null' else 'null'
            with self.subTest(first_released=released):
                self.state = support.scratch_dir(self, "rid-")
                self.write_older_file(
                    '{"boot":null,"connections":['
                    f'{{"device":"d1","id":["s","\\ud83d\\ude01"],"released":{released}}},'
                    f'{{"device":"d1","id":["s","\\ud83d\\ude01"],"released":{other}}}],'
                    '"devices":{"d1":999000.0},"version":1}')
                book = self.book(revoked_connection_keep_s=10)
                self.paired(book, "dnew")
                self.assertEqual(book.connect("dnew", JOINED_2).detail, REVOKED_CONNECTION_HELD)
                self.assertEqual(book.revoke("dnew"), ())
                self.paired(book, "dnew2")
                self.assertEqual(book.connect("dnew2", JOINED_2).detail, REVOKED_CONNECTION_HELD)


if __name__ == "__main__":
    unittest.main()
