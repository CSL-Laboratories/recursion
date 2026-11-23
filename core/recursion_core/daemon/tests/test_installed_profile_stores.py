"""An installed profile refuses to start when the breaker store or the ledger is missing.

On the tip this file was written against, ``compose_control`` calls ``open_breaker_store``,
which creates a missing breaker store, and a missing ledger is not a start refusal. The
ledger in the success case is built with a test-only key. ``install.json``'s
``authority_anchor`` is not involved.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon.launcher import EngineUnitManager
from recursion_core.daemon_install import Roots, install
from recursion_core.daemon_install.tests.test_daemon_install import EXEC, POSIX_PATHS
from recursion_core.daemon_install.tests.mount_stub import setUpModule, tearDownModule  # noqa: F401
from recursion_core.plugin_security import stores

TEST_ANCHOR = '11' * 32
LINUX = os.name == 'posix' and POSIX_PATHS


class QuietManager(EngineUnitManager):
    """An ``EngineUnitManager`` that does not ask the user manager. ``sweep`` lists no units."""

    def owned_units(self):
        return set()


@unittest.skipUnless(LINUX, 'the installer and the daemon stores are Linux-only')
class InstalledProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rc-installed-profile-')
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.mkdir(self.home, 0o700)
        self.roots = Roots(home=self.home)
        install(self.roots, EXEC)
        self.state = os.path.join(self.home, '.local', 'state', 'recursion')
        self.store_dir = os.path.join(self.state, 'plugin-security')
        self.breaker = os.path.join(self.store_dir, 'breaker.sqlite3')
        self.ledger = os.path.join(self.store_dir, 'ledger.sqlite3')
        self.runtime = os.path.join(self.tmp, 'rt')
        os.mkdir(self.runtime, 0o700)
        self.environ = {
            'PATH': '/usr/bin:/bin', 'HOME': self.home, 'LANG': 'C.UTF-8',
            'XDG_RUNTIME_DIR': self.runtime,
            'XDG_CONFIG_HOME': os.path.join(self.tmp, 'config'),
        }
        self.open = []
        self.addCleanup(self._close)

    def _close(self):
        while self.open:
            daemon, composed = self.open.pop()
            try:
                if composed is not None:
                    composed.close()
            finally:
                daemon.close()

    def _drop(self, path):
        for suffix in ('', '-wal', '-shm', '-journal'):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass

    def start(self, state=None):
        """Start the composed daemon. A ``SystemExit`` is the refusal: the daemon is closed first."""
        state = self.state if state is None else state
        daemon = Daemon(Registry(state), QuietManager(), boot_id=lambda: 'test-boot')
        daemon.sweep()
        try:
            composed = adapter.compose_control(daemon, state, environ=self.environ, routes={})
        except BaseException:
            daemon.close()
            raise
        self.open.append((daemon, composed))
        return daemon, composed

    def test_a_missing_breaker_store_is_not_created_and_start_is_refused(self):
        self._drop(self.breaker)
        self.assertFalse(os.path.lexists(self.ledger))  # install does not create it either
        try:
            self.start()
        except SystemExit as exc:
            text = str(exc)
            self.assertNotIn('Traceback', text)
            self.assertNotIn('\n', text)
            self.assertIn('breaker store is missing', text)
            self.assertIn('needs the anchor key, which recursiond install makes', text)
            self.assertFalse(os.path.lexists(self.breaker), text)
            self.assertFalse(os.path.lexists(self.ledger), text)
            with self.assertRaises(SystemExit) as again:  # a second caller still refuses, and still creates nothing
                self.start()
            self.assertIn('breaker store is missing', str(again.exception))
            self.assertFalse(os.path.lexists(self.breaker))
            return
        names = sorted(os.listdir(self.store_dir))
        self.fail('daemon started with the breaker store missing and created it; '
                  f'breaker_exists={os.path.lexists(self.breaker)} ledger_exists={os.path.lexists(self.ledger)} '
                  f'store_dir={names}')

    def test_a_missing_ledger_refuses_to_start(self):
        stores.create_ledger(self.ledger, TEST_ANCHOR)  # test-only key, then the file is removed
        self._drop(self.ledger)
        breaker_inode = os.lstat(self.breaker).st_ino
        try:
            self.start()
        except SystemExit as exc:
            text = str(exc)
            self.assertNotIn('Traceback', text)
            self.assertNotIn('\n', text)
            self.assertIn('authority ledger is missing', text)
            self.assertIn('needs the anchor key, which recursiond install makes', text)
            self.assertNotIn('breaker store is missing', text)
            self.assertFalse(os.path.lexists(self.ledger), text)
            self.assertEqual(os.lstat(self.breaker).st_ino, breaker_inode)
            return
        self.fail('daemon started with the ledger missing and was not refused; '
                  f'breaker_exists={os.path.lexists(self.breaker)} ledger_exists={os.path.lexists(self.ledger)}')

    def test_both_stores_present_with_a_test_only_key_starts(self):
        stores.create_ledger(self.ledger, TEST_ANCHOR)
        breaker_inode = os.lstat(self.breaker).st_ino
        ledger_inode = os.lstat(self.ledger).st_ino
        _daemon, composed = self.start()
        self.assertTrue(composed.server.path)
        self.assertEqual(os.lstat(self.breaker).st_ino, breaker_inode)
        self.assertEqual(os.lstat(self.ledger).st_ino, ledger_inode)

    def test_a_state_folder_that_is_not_an_installed_profile_still_creates_the_breaker_store(self):
        state = os.path.join(self.tmp, 'state')
        os.mkdir(state, 0o700)
        _daemon, _composed = self.start(state)
        created = os.path.join(state, 'plugin-security', 'breaker.sqlite3')
        self.assertTrue(os.path.lexists(created))
        self.assertFalse(os.path.lexists(os.path.join(state, 'plugin-security', 'ledger.sqlite3')))


if __name__ == '__main__':
    unittest.main()
