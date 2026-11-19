"""The installer's ledger step. The anchor key is not chosen here.

DL-12: the installer creates the authority ledger and the breaker store. The breaker store
is already created by ``install`` on this branch. The ledger stays behind ``anchor_public_key``,
which ``recursiond install`` cannot fill until that key is decided. With no key the step
reports ``needs the anchor key, which recursiond install makes`` and creates nothing. A test passes a test-only key
to show the call to ``stores.create_ledger`` (which calls ``authority.create_ledger``).
``install.json``'s ``authority_anchor`` is not read.
These tests are Linux-only: on Windows install and upgrade refuse ``platform_unsupported``
before they read a path, and test_unit_surrogate.py tests that refusal.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest

from recursion_core.daemon import plugins as daemon_plugins
from recursion_core.daemon_install import InstallRefused, Roots, install, owned_paths, upgrade
from recursion_core.daemon_install.tests.test_daemon_install import EXEC, POSIX_PATHS
from recursion_core.plugin_security import stores
from recursion_core.plugin_security.signing import key_id

# A test-only public key: 32 bytes of hex, not a key this installer chooses.
TEST_ANCHOR = "11" * 32
NEEDS_ANCHOR = "needs the anchor key, which recursiond install makes"


@unittest.skipUnless(POSIX_PATHS, "the unit's interpreter path is not absolute on this host")
@unittest.skipUnless(os.name == "posix", "the installer is Linux-only; on Windows install and upgrade refuse "
                     "platform_unsupported, tested in test_unit_surrogate.py")
class LedgerStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ledger-step-")
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")
        os.mkdir(self.home, 0o700)
        self.roots = Roots(home=self.home)
        self.paths = owned_paths(self.roots)
        # The path the daemon opens, spelled out so this setUp does not depend on the field the
        # step adds. test_the_ledger_path_is_the_daemons_own holds the two spellings together.
        self.ledger = os.path.join(self.home, ".local", "state", "recursion", "plugin-security", "ledger.sqlite3")

    def test_the_ledger_path_is_the_daemons_own(self) -> None:
        self.assertEqual(
            os.path.join(self.paths.state, daemon_plugins.STORE_DIR, daemon_plugins.LEDGER_FILE),
            self.paths.ledger)

    def test_without_an_anchor_the_step_reports_and_creates_nothing(self) -> None:
        plan = install(self.roots, EXEC)
        self.assertEqual(plan.ledger, (self.ledger, False, NEEDS_ANCHOR),
                         f"ledger step was {plan.ledger!r}; file exists={os.path.lexists(self.ledger)}")
        self.assertFalse(os.path.lexists(self.ledger))
        self.assertEqual(plan.stores, (
            ("breaker_store", self.paths.breaker_store, True),
            ("revocation_store", self.paths.revocation_store, True)))
        self.assertEqual(sorted(os.listdir(self.paths.store_dir)), ["breaker.sqlite3", "revoked.sqlite3"])
        # install.json is not written, and a planted authority_anchor is not the key.
        planted = os.path.join(self.home, ".config", "recursion", "plugins")
        os.makedirs(planted, 0o700)
        with open(os.path.join(planted, "install.json"), "w", encoding="utf-8") as handle:
            handle.write('{"authority_anchor": "' + TEST_ANCHOR + '"}')
        again = install(self.roots, EXEC)
        self.assertEqual(again.ledger, (self.ledger, False, NEEDS_ANCHOR))
        self.assertFalse(os.path.lexists(self.ledger))
        upgraded = upgrade(self.roots, EXEC)
        self.assertEqual(upgraded.ledger, (self.ledger, False, NEEDS_ANCHOR))
        self.assertFalse(os.path.lexists(self.ledger))

    def test_a_passed_anchor_creates_the_ledger_and_a_second_call_keeps_it(self) -> None:
        plan = install(self.roots, EXEC, anchor_public_key=TEST_ANCHOR)
        self.assertEqual(plan.ledger, (self.ledger, True, ""))
        self.assertEqual(stat.S_IMODE(os.lstat(self.ledger).st_mode) & 0o777, 0o600)
        db = stores.open_store(self.ledger, stores.LEDGER_MAGIC)
        try:
            anchor = db.execute("SELECT v FROM meta WHERE k='anchor'").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(anchor, key_id(TEST_ANCHOR))
        again = install(self.roots, EXEC, anchor_public_key=TEST_ANCHOR)
        self.assertEqual(again.ledger, (self.ledger, False, ""))
        self.assertFalse(again.changed)
        # Once the file is there, a later install that still has no key checks it and does not
        # report the missing key: creating it is no longer the step that is blocked.
        kept = upgrade(self.roots, EXEC)
        self.assertEqual(kept.ledger, (self.ledger, False, ""))

    def test_upgrade_with_an_anchor_creates_a_ledger_install_could_not(self) -> None:
        install(self.roots, EXEC)
        self.assertFalse(os.path.lexists(self.ledger))
        plan = upgrade(self.roots, EXEC, anchor_public_key=TEST_ANCHOR)
        self.assertEqual(plan.ledger, (self.ledger, True, ""))
        self.assertTrue(plan.changed)
        stores.open_store(self.ledger, stores.LEDGER_MAGIC).close()

    def test_a_key_that_is_not_a_public_key_creates_no_ledger(self) -> None:
        with self.assertRaises(InstallRefused) as caught:
            install(self.roots, EXEC, anchor_public_key="not-a-key")
        self.assertFalse(os.path.lexists(self.ledger))
        self.assertIn("malformed_public_key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
