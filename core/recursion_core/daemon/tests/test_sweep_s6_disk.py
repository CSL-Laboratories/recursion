"""P3: an existing breaker store is private when it is opened again.

``test_f09_existing_breaker_store_is_private_on_reopen``: group or other access is
tightened to 0600. A refused reopen is acceptable only when that access is already gone.
``test_a_stricter_0400_store_is_not_widened``: a read-only store stays 0400.
``test_a_stricter_0000_store_is_not_called_untrusted``.
``test_a_chmod_that_cannot_happen_names_the_refusal`` and
``test_an_open_that_cannot_chmod_names_the_refusal``: the tighten path names why it refused.
Fixture bytes are generated here.
"""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon import breaker as dbreaker


class DiskRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='s6-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_f09_existing_breaker_store_is_private_on_reopen(self):
        stored, _ = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        stored.close()
        path = dbreaker.store_path(self.root)
        path.chmod(0o666)
        try:
            stored, created = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        except dbreaker.Refused:
            left = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(left & 0o077, 0, 'a refused reopen left the store accessible to group or other')
            return
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, 'reopened breaker store keeps group/other read/write bits')
            self.assertFalse(created)
        finally:
            stored.close()

    def _reopen_at(self, mode):
        stored, _ = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        stored.close()
        path = dbreaker.store_path(self.root)
        path.chmod(mode)
        return path

    def test_a_stricter_0400_store_is_not_widened(self):
        path = self._reopen_at(0o400)
        stored, created = dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        try:
            self.assertFalse(created)
            self.assertFalse(stored.tripped('subscription:login-a'))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400,
                             'a read-only store was given owner write')
        finally:
            stored.close()

    def test_a_stricter_0000_store_is_not_called_untrusted(self):
        path = self._reopen_at(0o000)
        with self.assertRaises(dbreaker.Refused) as caught:
            dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o000)
        self.assertNotEqual(caught.exception.code, 'breaker_store_untrusted')
        self.assertEqual(caught.exception.code, 'breaker_store_invalid')

    def test_a_chmod_that_cannot_happen_names_the_refusal(self):
        path = self._reopen_at(0o666)

        def fail_chmod(fd, mode):
            raise OSError('chmod cannot happen')

        with mock.patch('recursion_core.daemon.breaker.os.fchmod', fail_chmod):
            with self.assertRaises(dbreaker.Refused) as caught:
                dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        self.assertEqual(caught.exception.code, 'breaker_store_not_private')
        self.assertIn('could not be made 0600', caught.exception.detail)
        self.assertNotEqual(caught.exception.detail, path.name)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0o066)

    def test_an_open_that_cannot_chmod_names_the_refusal(self):
        path = self._reopen_at(0o666)
        real_open = os.open

        def fail_nofollow(file, flags, *args, **kwargs):
            if flags & os.O_NOFOLLOW:
                raise OSError('open cannot happen')
            return real_open(file, flags, *args, **kwargs)

        with mock.patch('recursion_core.daemon.breaker.os.open', fail_nofollow):
            with self.assertRaises(dbreaker.Refused) as caught:
                dbreaker.open_breaker_store(self.root, clock=lambda: 10.0)
        self.assertEqual(caught.exception.code, 'breaker_store_not_private')
        self.assertIn('could not be made 0600', caught.exception.detail)
        self.assertNotEqual(caught.exception.detail, path.name)


if __name__ == '__main__':
    unittest.main()
