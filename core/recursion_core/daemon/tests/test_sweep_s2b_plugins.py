"""E21: a symlinked revocation store is not opened.

``test_a_symlinked_revocation_store_must_not_be_opened``. Fixture bytes are generated here.
"""
import os
from pathlib import Path
import tempfile
import unittest

from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security import create_revocation_store


class GilOff:
    def gil_enabled(self):
        return False


class PluginRegressions(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix='sweep-s2b-')
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def host(self, source, **options):
        config = self.root / 'config'
        fx.write_install(config, grants=[fx.grant_for(source)])
        fx.write_plugin(config / 'plugins' / 'fixture', source)
        options.setdefault('gil_probe', GilOff())
        host = plugins.PluginHost(config, self.root / 'state', verification_available=lambda: True, **options)
        self.addCleanup(host.close)
        return host

    def test_a_symlinked_revocation_store_must_not_be_opened(self):
        os.chmod(self.root, 0o711)
        shared = self.root / 'shared'
        shared.mkdir(mode=0o777)
        os.chmod(shared, 0o777)
        target = shared / 'other-revocations.sqlite3'
        create_revocation_store(str(target))
        os.chmod(target, 0o666)
        state_dir = self.root / 'state' / 'plugin-security'
        state_dir.mkdir(parents=True, mode=0o700)
        (state_dir / 'revoked.sqlite3').symlink_to(target)
        host = self.host(b'VALUE = 1\n')
        self.assertEqual(host.records[0]['state'], 'refused', 'loaded through a symlinked revocation store')


if __name__ == '__main__':
    unittest.main()
