"""DL-50 D4 on the daemon's plugin host.

PRODUCT.md d84cdd26 (CSLR-D-012) section 4.1: a revocation store that goes missing after
the installer created it refuses every plugin, and an untrusted plugin config folder
refuses every plugin, including folders passed in as ``plugin_dirs``.

The GIL probe and the verification flag are simulated, as in ``test_adv2_plugins.py``,
so the same checks run on every interpreter. No real key is used.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security import create_revocation_store
from recursion_core.plugin_security.tests.support import FakeGilProbe

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: the plugin host checks file ownership by uid'
REVOKED_KEY = 'ed25519:' + 'ab' * 16


def _source(marker):
    return f'open({str(marker)!r}, "w").close()\n'.encode() + b'VALUE = 1\n'


@unittest.skipUnless(LINUX, LINUX_ONLY)
class StoreAndConfigTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-plugin-trust-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config = self.tmp / 'config' / 'recursion'
        self.state = self.tmp / 'state'
        self.plugins = self.config / 'plugins'
        self.inside = self.tmp / 'inside-ran'
        self.outside = self.tmp / 'outside-ran'

    def host(self, **options):
        options.setdefault('gil_probe', FakeGilProbe())
        options.setdefault('verification_available', lambda: True)
        host = plugins.PluginHost(self.config, self.state, **options)
        self.addCleanup(host.close)
        return host

    def plant_store(self):
        directory = self.state / 'plugin-security'
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
        path = directory / 'revoked.sqlite3'
        create_revocation_store(str(path))
        db = sqlite3.connect(path)
        try:
            db.execute('INSERT INTO revoked (key_id, generation) VALUES (?, ?)', (REVOKED_KEY, 3))
        finally:
            db.close()
        return path

    def test_a_missing_revocation_store_refuses_every_plugin_and_is_not_recreated(self):
        # The installer created the store and a revoked key was recorded. The file then
        # went away. Recreating it empty would trust that key again, so the host must
        # refuse every plugin and leave the path absent.
        fx.write_install(self.config, grants=[fx.grant_for(_source(self.inside), plugin_id='fixture.inside')])
        fx.write_plugin(self.plugins / 'inside', _source(self.inside), plugin_id='fixture.inside')
        explicit = self.tmp / 'explicit'
        fx.write_plugin(explicit, _source(self.outside), plugin_id='fixture.outside')
        path = self.plant_store()
        os.unlink(path)
        for suffix in ('-wal', '-shm'):
            leftover = Path(str(path) + suffix)
            if leftover.exists():
                leftover.unlink()
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR') as logs:
            host = self.host(plugin_dirs=[str(explicit)])
        self.assertTrue(any('revocation_store_missing' in line for line in logs.output))
        self.assertFalse(path.exists(), 'a missing revocation store was created again')
        self.assertEqual([record['state'] for record in host.records], ['refused', 'refused'])
        self.assertEqual(sorted(record['code'] for record in host.records),
                         ['revocation_store_missing', 'revocation_store_missing'])
        self.assertFalse(self.inside.exists())
        self.assertFalse(self.outside.exists())
        self.assertIsNone(host.loader)
        # A second caller sees the same absence. Nothing in the first call wrote a store.
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR') as again_logs:
            again = self.host(plugin_dirs=[str(explicit)])
        self.assertTrue(any('revocation_store_missing' in line for line in again_logs.output))
        self.assertFalse(path.exists())
        self.assertEqual(sorted(record['code'] for record in again.records),
                         ['revocation_store_missing', 'revocation_store_missing'])
        self.assertFalse(self.outside.exists())

    def test_an_untrusted_config_folder_refuses_explicit_plugin_folders(self):
        # The config folder is group-writable. A separate operator-owned folder is named
        # with plugin_dirs. The store is present, so the only reason to refuse is the
        # config folder. Both plugins stay unloaded.
        inside = _source(self.inside)
        outside = _source(self.outside)
        fx.write_install(self.config, grants=[
            fx.grant_for(inside, plugin_id='fixture.inside'),
            fx.grant_for(outside, plugin_id='fixture.outside')])
        fx.write_plugin(self.plugins / 'inside', inside, plugin_id='fixture.inside')
        explicit = self.tmp / 'explicit'
        fx.write_plugin(explicit, outside, plugin_id='fixture.outside')
        os.chmod(explicit, 0o700)
        self.plant_store()
        os.chmod(self.plugins, 0o770)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR') as logs:
            host = self.host(plugin_dirs=[str(explicit)])
        self.assertTrue(any('every plugin is refused' in line for line in logs.output))
        by_source = {Path(record['source']).name: record for record in host.records}
        self.assertEqual(by_source['plugins']['code'], 'plugin_directory_untrusted')
        self.assertEqual(by_source['explicit']['state'], 'refused')
        self.assertEqual(by_source['explicit']['code'], 'plugin_directory_untrusted')
        self.assertEqual(by_source['explicit']['detail'], 'install directory')
        self.assertNotIn('inside', by_source)
        self.assertFalse(self.inside.exists())
        self.assertFalse(self.outside.exists())
        self.assertIsNone(host.loader)

    def test_a_trusted_config_still_loads_an_explicit_plugin_folder(self):
        # No config folder at all: an operator-owned --plugin folder still loads.
        # The store is the installer's, already on disk.
        source = _source(self.outside)
        explicit = self.tmp / 'explicit'
        fx.write_plugin(explicit, source, plugin_id='fixture.outside')
        os.chmod(explicit, 0o700)
        self.plant_store()
        host = self.host(plugin_dirs=[str(explicit)])
        record, = host.records
        self.assertEqual((record['state'], record['plugin_id']), ('loaded', 'fixture.outside'))
        self.assertTrue(self.outside.exists())


if __name__ == '__main__':
    unittest.main()
