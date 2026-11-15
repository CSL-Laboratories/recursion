"""Store startup I/O failures use the existing typed refusals.

No signature is made or checked here: the ledger only has to be bound to an anchor in the right
format, so a fixed public value stands in for one and the test needs no ``cryptography``.
"""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from recursion_core.plugin_security import (
    Authority, ConnectionBreaker, Refused, TrustStore,
    create_breaker_store, create_ledger, create_revocation_store,
)

ANCHOR = "11" * 32  # a public value in the anchor's format; nothing signs with it or verifies against it


@unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                     "requires Unix file modes and a non-root uid")
class StoreOpenErrorsTests(unittest.TestCase):
    def test_unreadable_existing_stores_raise_typed_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = (
                ("revoked", create_revocation_store,
                 lambda p: TrustStore({}, revocation_store=p), "revocation_store_invalid"),
                ("ledger", lambda p: create_ledger(p, ANCHOR),
                 lambda p: Authority(ANCHOR, p, lambda: 0), "ledger_invalid"),
                ("breaker", create_breaker_store, ConnectionBreaker, "breaker_store_invalid"),
            )
            for name, create, open_store, expected in cases:
                with self.subTest(store=name):
                    path = str(Path(directory) / (name + ".sqlite3"))
                    create(path)
                    os.chmod(path, 0)
                    try:
                        with self.assertRaises(Refused) as caught:
                            open_store(path)
                        self.assertEqual(caught.exception.code, expected)
                    finally:
                        os.chmod(path, 0o600)

    def test_a_read_only_ledger_from_before_the_index_raises_the_typed_refusal(self):
        # The ledger can be read, and its anchor matches; it has no index yet and cannot be written, so
        # the statement that would give it one fails after the open succeeded.
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ledger.sqlite3")
            create_ledger(path, ANCHOR)
            db = sqlite3.connect(path)
            try:
                db.execute("DROP INDEX charges_by_profile")
                db.execute("PRAGMA journal_mode=DELETE")  # a write-ahead log cannot be read read-only
                db.commit()
            finally:
                db.close()
            os.chmod(path, 0o400)
            try:
                with self.assertRaises(Refused) as caught:
                    Authority(ANCHOR, path, lambda: 0)
                self.assertEqual(caught.exception.code, "ledger_invalid")
            finally:
                os.chmod(path, 0o600)


class LedgerIndexErrorsTests(unittest.TestCase):
    """The ledger opens and its anchor matches; the failure comes one statement later, where a ledger
    from before the index is given it. That is a typed refusal too, and not SQLite's own error. No file
    mode is involved, so this runs on every platform and as root."""

    def test_a_ledger_without_its_charges_table_raises_the_typed_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ledger.sqlite3")
            create_ledger(path, ANCHOR)
            db = sqlite3.connect(path)
            try:
                db.execute("DROP TABLE charges")  # its index goes with it
                db.commit()
            finally:
                db.close()
            with self.assertRaises(Refused) as caught:
                Authority(ANCHOR, path, lambda: 0)
            self.assertEqual(caught.exception.code, "ledger_invalid")


if __name__ == "__main__":
    unittest.main()
