"""Store startup I/O failures use the existing typed refusals.

No signature is made or checked here: the ledger only has to be bound to an anchor in the right
format, so a fixed public value stands in for one and the test needs no ``cryptography``.
"""
import os
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
