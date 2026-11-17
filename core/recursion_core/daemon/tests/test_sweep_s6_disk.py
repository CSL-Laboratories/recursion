"""P3: an existing breaker store is private when it is opened again.

``test_f09_existing_breaker_store_is_private_on_reopen``. Fixture bytes are generated here.
"""
from pathlib import Path
import stat
import tempfile
import unittest

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
            return
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, 'reopened breaker store keeps group/other read/write bits')
        finally:
            stored.close()


if __name__ == '__main__':
    unittest.main()
