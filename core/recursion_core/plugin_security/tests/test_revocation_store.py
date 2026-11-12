"""Round 2 finding 1: a key revoked at runtime stays revoked after a restart (PRODUCT.md 4.4).

These tests need no signing, so they also run where ``cryptography`` is missing.
"""

from __future__ import annotations

import os
import sqlite3
import unittest

from recursion_core.plugin_security import Refused, TrustStore, create_revocation_store
from recursion_core.plugin_security.tests.support import temp_dir

KID = "ed25519:" + "ab" * 16


class RevocationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(temp_dir(self), "revoked.db")
        create_revocation_store(self.path)

    def _open(self, **kwargs) -> TrustStore:
        trust = TrustStore({}, revocation_store=self.path, **kwargs)
        self.addCleanup(trust.close)
        return trust

    def _refused(self, code: str, fn, *args, **kwargs) -> None:
        with self.assertRaises(Refused) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_restart_keeps_a_runtime_revocation(self) -> None:
        before = self._open()
        self.assertNotIn(KID, before.revoked)
        self.assertEqual(before.revoke(KID), 1)
        self.assertEqual(before.revoke(KID), 2)  # a repeat advances the generation
        before.close()
        after = self._open()  # a restart: installer config never saw the revocation
        self.assertIn(KID, after.revoked)

    def test_config_and_store_revocations_are_combined(self) -> None:
        self._open().revoke(KID)
        other = "ed25519:" + "cd" * 16
        self.assertEqual(self._open(revoked_key_ids=frozenset({other})).revoked, frozenset({KID, other}))

    def test_without_a_store_revoke_applies_in_memory_and_refuses(self) -> None:
        trust = TrustStore({})
        self._refused("revocation_not_durable", trust.revoke, KID)
        self.assertIn(KID, trust.revoked)

    def test_missing_or_invalid_store_refuses_and_create_is_installer_only(self) -> None:
        self._refused("revocation_store_missing", TrustStore, {},
                      revocation_store=os.path.join(temp_dir(self), "absent.db"))
        junk = os.path.join(temp_dir(self), "junk.db")
        with open(junk, "wb") as fh:
            fh.write(b"not a database")
        self._refused("revocation_store_invalid", TrustStore, {}, revocation_store=junk)
        self.assertRaises(FileExistsError, create_revocation_store, self.path)

    def test_sqlite_error_on_revoke_is_revocation_not_durable(self) -> None:
        trust = self._open()
        trust._db.execute("PRAGMA query_only=ON")
        self._refused("revocation_not_durable", trust.revoke, KID)
        self.assertNotIn(KID, trust.revoked)
        self.assertIsNone(trust.generation(KID))

    def test_an_older_store_without_generation_migrates(self) -> None:
        path = os.path.join(temp_dir(self), "old.db")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        db = sqlite3.connect(path, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.execute("CREATE TABLE revoked (key_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO meta VALUES ('magic', 'csl-revocation-store/v0')")
        db.execute("INSERT INTO revoked (key_id) VALUES (?)", (KID,))
        db.close()
        trust = TrustStore({}, revocation_store=path)
        self.addCleanup(trust.close)
        self.assertEqual(trust.generation(KID), 1)
        self.assertEqual(trust.revoke(KID), 2)
        trust.close()
        again = TrustStore({}, revocation_store=path)
        self.addCleanup(again.close)
        self.assertEqual(again.generation(KID), 2)


if __name__ == "__main__":
    unittest.main()
