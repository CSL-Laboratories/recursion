import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.child_env import EnvRefused, build_child_env, policy


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s7-child-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        self.bases = mock.patch.object(policy, '_tmp_bases', return_value=[str(self.root)])
        self.bases.start()
        self.addCleanup(self.bases.stop)
        self.cache = mock.patch.dict(policy._TMP, {}, clear=True)
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def build(self, **kw):
        return build_child_env(kw.pop('engine', 'claude_code'), home=kw.pop('home', str(self.home)),
                               auth_mode=kw.pop('auth_mode', 'subscription'), parent=kw.pop('parent', {}), **kw)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class Defects(Fixture):
    def test_D07_mapping_shapes(self):
        shapes = (
            {'overrides': 'bad'},
            {'overrides': []},
            {'overrides': True},
            {'overrides': False},
            {'overrides': {'LANG': 1}},
            {'overrides': {'LANG': True}},
            {'overrides': {1: 'C'}},
        )
        for kw in shapes:
            with self.subTest(input=repr(kw)):
                with self.assertRaises(EnvRefused) as caught:
                    self.build(**kw)
                self.assertIsNone(caught.exception.name)
                self.assertEqual('override_malformed', caught.exception.cause)


if __name__ == "__main__":
    unittest.main(verbosity=2)
