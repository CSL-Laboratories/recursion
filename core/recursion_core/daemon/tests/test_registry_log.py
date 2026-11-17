"""A failed reconcile after a failed launch must not log the launch argv.

E16: Daemon.start catches a launch failure. When the follow-up reconcile also
fails, log.exception records the chained CalledProcessError, and that error's
text is the whole launch argv.
"""
import io
import logging
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import recursion_core.daemon as core

LINUX = sys.platform.startswith('linux')


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class RegistryLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='registry-log-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def store(self, directory=None):
        opened = core.Registry(directory or self.root / 'state')
        self.addCleanup(opened.close)
        return opened

    def test_F8_reconcile_failure_log_does_not_include_launch_argv(self):
        store = self.store()
        marker = 'SYNTHETIC-NOT-A-CREDENTIAL'
        manager = core.SystemdUserManager()

        def call(argv):
            if argv[0] == 'systemd-run':
                raise subprocess.CalledProcessError(1, argv)
            return ('LoadState=not-found\nActiveState=inactive\nMainPID=0\n'
                    'ControlGroup=\n')

        manager._call = call
        daemon = core.Daemon(store, manager, lambda: 'boot-a')
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        core.log.addHandler(handler)
        try:
            with patch.object(store, 'state',
                              side_effect=sqlite3.OperationalError('test disk full')):
                with self.assertRaises(subprocess.CalledProcessError):
                    daemon.start('one', ['not-executed', '--api-key', marker])
        finally:
            core.log.removeHandler(handler)
            handler.close()
        self.assertNotIn(marker, stream.getvalue())


if __name__ == '__main__':
    unittest.main()
