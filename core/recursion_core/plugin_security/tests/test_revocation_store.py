"""Round 2 finding 1: a key revoked at runtime stays revoked after a restart (PRODUCT.md 4.4).

These tests need no signing, so they also run where ``cryptography`` is missing.
"""

from __future__ import annotations

import os
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
        before.revoke(KID)
        before.revoke(KID)  # idempotent
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


if __name__ == "__main__":
    unittest.main()
