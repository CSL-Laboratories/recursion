"""A PATH directory that stat accepts and listdir cannot read is a typed refusal.

PATH_TOOLS is None only in this witness: that is the mode that lists the directory.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.child_env import EnvRefused, build_child_env, policy


@unittest.skipIf(os.name == "nt", "POSIX rules")
class PathListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='s7-path-')
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
        return build_child_env(
            'claude_code',
            home=str(self.home),
            auth_mode='subscription',
            parent=kw.pop('parent', {}),
            **kw,
        )

    def test_an_unreadable_path_directory_is_value_unreadable(self):
        blocked = self.root / 'blocked'
        blocked.mkdir(mode=0o300)
        with mock.patch.object(policy, 'PATH_TOOLS', None):
            with self.assertRaises(EnvRefused) as caught:
                self.build(parent={'PATH': str(blocked)})
        self.assertEqual('PATH', caught.exception.name)
        self.assertEqual('value_unreadable', caught.exception.cause)
        self.assertNotIn(str(blocked), str(caught.exception))

    def test_a_readable_path_directory_is_still_accepted(self):
        open_dir = self.root / 'open'
        open_dir.mkdir(mode=0o700)
        with mock.patch.object(policy, 'PATH_TOOLS', None):
            env = self.build(parent={'PATH': str(open_dir)})
        self.assertEqual(str(open_dir), env['PATH'])

    def test_listdir_interrupt_propagates(self):
        open_dir = self.root / 'open'
        open_dir.mkdir(mode=0o700)
        with mock.patch.object(policy, 'PATH_TOOLS', None):
            with mock.patch.object(policy.os, 'listdir', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.build(parent={'PATH': str(open_dir)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
