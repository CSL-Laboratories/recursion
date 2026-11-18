"""``plugin.status`` names the right plugin, and answers a malformed id as a malformed request.

Two answers of ``PluginHost`` checked apart from the Linux file reads, so they run on every host:

- an id outside the published grammar (schemas/control/v1/methods.v1.json, ``plugin_id``) is
  ``request_invalid``, never ``plugin_unknown``; a well-formed id nobody installed stays
  ``plugin_unknown``;
- once the core is poisoned, a later refused directory does not carry the findings, the id or the
  registry label of the plugin that poisoned it.

The host's file reads (uid ownership, O_NOFOLLOW) are Linux-only and are replaced here by bytes in
memory; the loader is the real ``PluginLoader``. The GIL probe is SIMULATED (FakeGilProbe): it is not
evidence about a real extension turning the GIL on.
"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon import plugins
from recursion_core.plugin_security import CoreHealth, PluginLoader, ReservedEffects, TrustStore, canonical
from recursion_core.plugin_security.tests.support import FakeGilProbe

SOURCE = b'VALUE = 1\n'


def _manifest(plugin_id, source=SOURCE):
    return canonical({'schema': 'csl-plugin-manifest/vNext', 'plugin_id': plugin_id, 'version': '1.0.0',
                      'artifact_sha256': hashlib.sha256(source).hexdigest(), 'dependency_lock_sha256': '0' * 64,
                      'install_scripts': [], 'developer_key': '33' * 32, 'permissions': ['read'],
                      'network_destinations': [], 'credential_refs': []})


class _Rows:
    """The inventory ``PluginHost.status`` reads, and nothing else."""

    def __init__(self, rows):
        self.rows = rows

    def inventory(self):
        return {'plugins': [dict(row) for row in self.rows]}


class MalformedIdTests(unittest.TestCase):
    def test_an_id_outside_the_grammar_is_request_invalid(self):
        host = _Rows([{'plugin_id': 'notes.search', 'state': 'loaded', 'source': 'x'}])
        for plugin_id in ('Notes.Search', 'a', 'x' * 70, 'notes search', '1notes', 'notes.search\n',
                          '-notes', 'notes/search'):
            with self.subTest(plugin_id=plugin_id):
                self.assertEqual(plugins.PluginHost.status(host, plugin_id),
                                 {'code': 'request_invalid', 'detail': 'plugin_id'})

    def test_a_well_formed_id_not_installed_stays_plugin_unknown(self):
        host = _Rows([{'plugin_id': 'notes.search', 'state': 'loaded', 'source': 'x'}])
        for plugin_id in ('notes.other', 'ab', 'x' * 64):
            with self.subTest(plugin_id=plugin_id):
                self.assertEqual(plugins.PluginHost.status(host, plugin_id),
                                 {'code': 'plugin_unknown', 'detail': plugin_id})
        self.assertEqual(plugins.PluginHost.status(host, 'notes.search')['code'], 'ok')


class PoisonedCoreRowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.dir_a, self.dir_b = root / 'a', root / 'b'
        self.files = {}
        for folder, plugin_id in ((self.dir_a, 'plugin.a'), (self.dir_b, 'plugin.b')):
            self.files[folder / plugins.MANIFEST] = _manifest(plugin_id)
            self.files[folder / plugins.ARTIFACT] = SOURCE

    def _read(self, path, limit, *, missing_ok=False):
        data = self.files.get(Path(path))
        if data is None and missing_ok:
            return None
        return data

    def test_a_refusal_after_a_poison_names_no_earlier_plugin(self):
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        health = CoreHealth()
        # SIMULATED: the GIL reads on from the first read, so plugin.a's load poisons the core.
        host.loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), health, FakeGilProbe([True]))
        host._labels = {hashlib.sha256(self.files[self.dir_a / plugins.MANIFEST]).hexdigest(): 'verified'}
        with mock.patch.object(plugins, '_owned_directory', lambda path: True), \
                mock.patch.object(plugins, '_read_owned', self._read), \
                self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            row_a = host._load(self.dir_a)
            row_b = host._load(self.dir_b)
        self.assertIsNotNone(health.poisoned)
        self.assertEqual((row_a['code'], row_a['plugin_id']), ('core_poisoned_gil_enabled', 'plugin.a'))
        self.assertIn('verified (a label, not a gate)', [f['detail'] for f in row_a['findings']])
        self.assertEqual(row_b['code'], 'core_poisoned_restart_required')
        self.assertNotEqual(row_b['plugin_id'], 'plugin.a')
        self.assertEqual([f for f in row_b['findings'] if f.get('code') in ('manifest_ok', 'registry_label')], [])
        # plugin.status for plugin.a answers dir a's own row, and it is the only row with that id.
        rows = _Rows([row_a, row_b])
        answer = plugins.PluginHost.status(rows, 'plugin.a')
        self.assertEqual((answer['code'], answer['plugin']['source']), ('ok', str(self.dir_a)))
        self.assertEqual([row['source'] for row in rows.inventory()['plugins'] if row['plugin_id'] == 'plugin.a'],
                         [str(self.dir_a)])

    def _poisoned_host(self):
        host = plugins.PluginHost.__new__(plugins.PluginHost)
        health = CoreHealth()
        # SIMULATED: the GIL reads on from the first read, so plugin.a's load poisons the core.
        host.loader = PluginLoader(TrustStore({}), {}, ReservedEffects(), health, FakeGilProbe([True]))
        host._labels = {}
        return host, health

    def test_a_refusal_after_a_poison_carries_its_own_id_and_label(self):
        host, health = self._poisoned_host()
        dir_c = self.dir_a.parent / 'c'
        self.files[dir_c / plugins.MANIFEST] = _manifest('plugin.c')
        self.files[dir_c / plugins.ARTIFACT] = SOURCE
        # No plugin code runs after the poison: b's artifact would end the test process if it ran.
        ran = b'raise SystemExit("plugin.b ran")\n'
        self.files[self.dir_b / plugins.ARTIFACT] = ran
        self.files[self.dir_b / plugins.MANIFEST] = _manifest('plugin.b', ran)
        host._labels = {hashlib.sha256(self.files[self.dir_a / plugins.MANIFEST]).hexdigest(): 'verified',
                        hashlib.sha256(self.files[self.dir_b / plugins.MANIFEST]).hexdigest(): 'non_verified'}
        with mock.patch.object(plugins, '_owned_directory', lambda path: True), \
                mock.patch.object(plugins, '_read_owned', self._read), \
                self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            row_a = host._load(self.dir_a)
            row_b = host._load(self.dir_b)
            row_c = host._load(dir_c)
        self.assertIsNotNone(health.poisoned)
        self.assertEqual(row_a['plugin_id'], 'plugin.a')
        for row, plugin_id in ((row_b, 'plugin.b'), (row_c, 'plugin.c')):
            with self.subTest(plugin_id=plugin_id):
                self.assertEqual((row['state'], row['code'], row['plugin_id']),
                                 ('refused', 'core_poisoned_restart_required', plugin_id))
        # The label the install recorded for b's manifest bytes; none recorded for c's, so none on its row.
        self.assertEqual(row_b['registry_label'], 'non_verified')
        self.assertNotIn('registry_label', row_c)
        rows = _Rows([row_a, row_b, row_c])
        for row, plugin_id in ((row_b, 'plugin.b'), (row_c, 'plugin.c')):
            with self.subTest(status=plugin_id):
                answer = plugins.PluginHost.status(rows, plugin_id)
                self.assertEqual((answer['code'], answer['plugin']['source'], answer['plugin']['code']),
                                 ('ok', row['source'], 'core_poisoned_restart_required'))

    def test_after_a_poison_a_manifest_without_a_readable_id_gives_no_id(self):
        cases = {
            'unreadable': plugins.PluginRefused('plugin_file_unreadable', 'manifest.json: PermissionError'),
            'not a regular file': plugins.PluginRefused(
                'plugin_file_untrusted', 'manifest.json: must be an operator-owned regular file, not group/other '
                                         'writable'),
            'id outside the grammar': _manifest('Plugin.B'),
            'id too short': _manifest('b'),
            'not canonical': _manifest('plugin.b') + b' ',
        }
        for name, manifest in cases.items():
            with self.subTest(case=name):
                host, _ = self._poisoned_host()
                files = dict(self.files)
                files[self.dir_b / plugins.MANIFEST] = manifest
                host._labels = {} if isinstance(manifest, Exception) else {
                    hashlib.sha256(manifest).hexdigest(): 'verified'}

                def read(path, limit, *, missing_ok=False, files=files):
                    data = files.get(Path(path))
                    if isinstance(data, Exception):
                        raise data
                    return None if data is None and missing_ok else data

                with mock.patch.object(plugins, '_owned_directory', lambda path: True), \
                        mock.patch.object(plugins, '_read_owned', read), \
                        self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                    row_a = host._load(self.dir_a)
                    row_b = host._load(self.dir_b)
                expected = manifest.code if isinstance(manifest, Exception) else 'core_poisoned_restart_required'
                self.assertEqual((row_b['state'], row_b['code'], row_b['plugin_id']), ('refused', expected, None))
                self.assertNotIn('registry_label', row_b)
                self.assertEqual(plugins.PluginHost.status(_Rows([row_a, row_b]), 'plugin.b'),
                                 {'code': 'plugin_unknown', 'detail': 'plugin.b'})


if __name__ == '__main__':
    unittest.main()
