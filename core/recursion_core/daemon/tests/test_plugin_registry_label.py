"""The registry label on an install is a label, not a gate (PRODUCT.md 1234-1242, 1900).

``install.json`` may name the label the installer saw for one manifest, keyed by the SHA-256 of
that manifest's bytes: ``verified`` or ``non_verified``. No entry means unlisted. The daemon passes
the label that matches the installed manifest to the loader, and ``plugin.list`` and ``plugin.status``
return it. The label never changes a permission, a grant, the order permission, or whether a plugin
loads. Nothing here uses the network.
"""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security.tests.support import FakeGilProbe, needs_crypto

LINUX = os.name == 'posix' and os.uname().sysname == 'Linux'
LINUX_ONLY = ('Linux-only: the plugin host checks file ownership by uid; on Windows PluginHost refuses '
              'PlatformUnsupported, covered by daemon/tests/test_windows_refusal.py')
PERMS = ('read', 'panel', 'order')


def _digest(manifest):
    return hashlib.sha256(manifest).hexdigest()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class LabelReaderTests(unittest.TestCase):
    """``read_install`` keeps a label bound to a manifest digest and refuses any other value."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-label-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config = self.tmp / 'config' / 'recursion'
        self.plugins = self.config / 'plugins'

    def test_a_missing_file_and_a_file_with_no_label_carry_no_label(self):
        # Absent means unlisted: the reader reports no label, the same as no install file.
        missing = plugins.read_install(self.plugins)
        self.assertEqual(missing['registry_labels'], {})
        fx.write_install(self.config)
        omitted = plugins.read_install(self.plugins)
        self.assertEqual(omitted['registry_labels'], {})
        fx.write_install(self.config, registry_labels={})
        empty = plugins.read_install(self.plugins)
        self.assertEqual(empty['registry_labels'], {})

    def test_a_label_is_kept_bound_to_the_manifest_digest(self):
        digest = 'ab' * 32
        other = 'cd' * 32
        fx.write_install(self.config, registry_labels={digest: 'verified', other: 'non_verified'})
        install = plugins.read_install(self.plugins)
        self.assertEqual(install['registry_labels'], {digest: 'verified', other: 'non_verified'})
        # A second read does not share the dict the first caller holds.
        install['registry_labels']['ee' * 32] = 'verified'
        again = plugins.read_install(self.plugins)
        self.assertEqual(again['registry_labels'], {digest: 'verified', other: 'non_verified'})

    def test_an_unknown_label_value_is_refused(self):
        digest = 'ab' * 32
        bad = ('official', 'unlisted', 'VERIFIED', 'verified ', '', True, None, 1, ['verified'])
        for value in bad:
            with self.subTest(value=value):
                fx.write_install(self.config, registry_labels={digest: value})
                with self.assertRaises(plugins.PluginRefused) as ctx:
                    plugins.read_install(self.plugins)
                self.assertEqual(ctx.exception.code, 'plugin_install_config_invalid')
                self.assertEqual(ctx.exception.detail, 'registry_label')
        shapes = (
            ['verified'],
            'verified',
            {digest.upper(): 'verified'},
            {'abc': 'verified'},
            {'ab' * 32 + '0': 'verified'},
            {'zz' * 32: 'verified'},
            None,
            True,
        )
        for shape in shapes:
            with self.subTest(shape=shape):
                fx.write_install(self.config, registry_labels=shape)
                with self.assertRaises(plugins.PluginRefused) as ctx:
                    plugins.read_install(self.plugins)
                self.assertEqual(ctx.exception.detail, 'registry_label')


@unittest.skipUnless(LINUX, LINUX_ONLY)
@needs_crypto
class LabelLoadTests(unittest.TestCase):
    """A label is shown and decides nothing: trust, permissions and whether the plugin loads."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-label-load-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config = self.tmp / 'config' / 'recursion'
        self.state = self.tmp / 'state'
        self.plugins_dir = self.config / 'plugins'
        self.source = fx.OPERATOR_SOURCE

    def host(self):
        fx.ensure_revocation_store(self.state)
        host = plugins.PluginHost(self.config, self.state, gil_probe=FakeGilProbe())
        self.addCleanup(host.close)
        return host

    def install(self, ids, labels, *, granted=None):
        """Write one unsigned fixture per id. ``labels`` maps plugin id to verified, non_verified or None.

        None writes no entry. ``mismatch`` writes an entry whose digest is not this manifest's.
        ``granted`` names the ids that receive a grant; the default is every id. A verified label
        on an id with no grant must still grant nothing.
        """
        if granted is None:
            granted = set(ids)
        grants, bound = [], {}
        manifests = {}
        for plugin_id in ids:
            manifest = fx.write_plugin(self.plugins_dir / plugin_id, self.source, plugin_id=plugin_id,
                                       permissions=PERMS)
            manifests[plugin_id] = manifest
            if plugin_id in granted:
                grants.append(fx.grant_for(self.source, plugin_id=plugin_id, permissions=PERMS))
            label = labels.get(plugin_id)
            if label == 'mismatch':
                bound[_digest(manifest + b'\n')] = 'verified'
            elif label is not None:
                bound[_digest(manifest)] = label
        extra = {'registry_labels': bound} if bound else {}
        fx.write_install(self.config, grants=grants, **extra)
        return manifests

    def test_a_verified_label_on_an_unsigned_plugin_grants_nothing(self):
        # The plugin stays unsigned. Order stays off: the label is not a grant and not an acknowledgement.
        # label.bare has the verified label and no grant at all, so the label itself adds no permission.
        self.install(('label.verified', 'label.plain', 'label.bare'),
                     {'label.verified': 'verified', 'label.plain': None, 'label.bare': 'verified'},
                     granted={'label.verified', 'label.plain'})
        host = self.host()
        rows = {row['plugin_id']: row for row in host.records}
        verified, plain, bare = rows['label.verified'], rows['label.plain'], rows['label.bare']
        self.assertEqual(verified['state'], 'loaded')
        self.assertEqual(verified['trust'], 'unsigned')
        self.assertEqual(verified['registry_label'], 'verified')
        self.assertNotIn('order', verified['permissions'])
        self.assertEqual(verified['permissions'], ['read', 'panel'])
        self.assertEqual((plain['trust'], plain['permissions'], plain['state']),
                         (verified['trust'], verified['permissions'], 'loaded'))
        self.assertEqual(plain['registry_label'], 'unlisted')
        self.assertIn('verified (a label, not a gate)', [f['detail'] for f in verified['findings']])
        self.assertEqual((bare['state'], bare['trust'], bare['permissions'], bare['registry_label']),
                         ('loaded', 'unsigned', [], 'verified'))

    def test_a_label_whose_digest_does_not_match_is_shown_unlisted(self):
        self.install(('label.mismatch', 'label.plain'), {'label.mismatch': 'mismatch', 'label.plain': None})
        host = self.host()
        rows = {row['plugin_id']: row for row in host.records}
        mismatch, plain = rows['label.mismatch'], rows['label.plain']
        self.assertEqual(mismatch['state'], 'loaded')
        self.assertEqual(mismatch['registry_label'], 'unlisted')
        self.assertEqual((mismatch['trust'], mismatch['permissions']), (plain['trust'], plain['permissions']))
        self.assertNotIn('order', mismatch['permissions'])
        self.assertIn('unlisted (a label, not a gate)', [f['detail'] for f in mismatch['findings']])

    def test_an_unknown_label_value_refuses_the_install_and_loads_nothing(self):
        manifest = fx.write_plugin(self.plugins_dir / 'label.bad', self.source, plugin_id='label.bad',
                                   permissions=PERMS)
        fx.write_install(self.config, grants=[fx.grant_for(self.source, plugin_id='label.bad', permissions=PERMS)],
                         registry_labels={_digest(manifest): 'official'})
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual([(row['state'], row['code'], row['detail']) for row in host.records],
                         [('refused', 'plugin_install_config_invalid', 'registry_label')])
        self.assertIsNone(host.loader)

    def test_a_plugin_with_no_label_loads_exactly_as_one_with_an_empty_map(self):
        # The key omitted, and the key present but empty, are both unlisted, and the load matches.
        self.install(('label.plain',), {'label.plain': None})
        omitted = self.host()
        plain = omitted.records[0]
        self.assertEqual((plain['state'], plain['trust'], plain['registry_label'], plain['permissions']),
                         ('loaded', 'unsigned', 'unlisted', ['read', 'panel']))
        self.assertNotIn('order', plain['permissions'])
        omitted.close()
        fx.write_install(self.config, grants=[fx.grant_for(self.source, plugin_id='label.plain', permissions=PERMS)],
                         registry_labels={})
        fx.ensure_revocation_store(self.tmp / 'state-empty')
        empty = plugins.PluginHost(self.config, self.tmp / 'state-empty', gil_probe=FakeGilProbe())
        self.addCleanup(empty.close)
        again = empty.records[0]
        self.assertEqual((again['state'], again['trust'], again['permissions'], again['registry_label']),
                         (plain['state'], plain['trust'], plain['permissions'], 'unlisted'))

    def test_the_label_does_not_decide_whether_a_plugin_loads(self):
        # A tampered artifact is still refused when the manifest's own label says verified.
        manifest = fx.write_plugin(self.plugins_dir / 'label.tamper', self.source, plugin_id='label.tamper',
                                   permissions=PERMS)
        (self.plugins_dir / 'label.tamper' / 'plugin.py').write_bytes(self.source + b'TAMPERED = True\n')
        os.chmod(self.plugins_dir / 'label.tamper' / 'plugin.py', 0o600)
        fx.write_install(self.config, grants=[fx.grant_for(self.source, plugin_id='label.tamper', permissions=PERMS)],
                         registry_labels={_digest(manifest): 'verified'})
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        row = host.records[0]
        self.assertEqual((row['state'], row['code']), ('refused', 'artifact_digest_mismatch'))

    def test_plugin_list_and_plugin_status_return_the_label(self):
        self.install(('label.verified', 'label.nonverified', 'label.plain'),
                     {'label.verified': 'verified', 'label.nonverified': 'non_verified', 'label.plain': None})
        host = self.host()
        from recursion_core.daemon import control_adapter as adapter
        control = adapter.DaemonControl(None, None, None, None, self.state, plugins=host)
        listed = {row['plugin_id']: row['registry_label'] for row in control.call('plugin.list')['plugins']}
        self.assertEqual(listed, {'label.verified': 'verified', 'label.nonverified': 'non_verified',
                                  'label.plain': 'unlisted'})
        for plugin_id, label in listed.items():
            answer = control.call('plugin.status', {'plugin_id': plugin_id})
            self.assertEqual(answer['code'], 'ok')
            self.assertEqual(answer['plugin']['registry_label'], label)
            self.assertEqual(answer['plugin']['trust'], 'unsigned')
            self.assertNotIn('order', answer['plugin']['permissions'])
        # A second call answers the same row. An unknown id is refused. A missing id is refused.
        again = control.call('plugin.status', {'plugin_id': 'label.verified'})
        self.assertEqual(again['plugin']['registry_label'], 'verified')
        self.assertEqual(control.call('plugin.status', {'plugin_id': 'label.missing'})['code'], 'plugin_unknown')
        self.assertEqual(control.call('plugin.status', {})['code'], 'request_invalid')
        self.assertEqual(control.call('plugin.status', None)['code'], 'request_invalid')

    def test_plugin_status_answers_the_loaded_row_whatever_the_directory_order(self):
        # A directory refused after its manifest parsed carries the id its own manifest claims.
        # For an id the loader holds active, plugin.status answers the loaded row, whichever directory
        # sorts first. A refused row is answered only for an id no loaded row carries. plugin.list
        # keeps every row.
        from recursion_core.daemon import control_adapter as adapter
        for copy_dir, real_dir in (('a-copy', 'b-real'), ('b-copy', 'a-real')):
            with self.subTest(refused=copy_dir, loaded=real_dir):
                config = self.tmp / copy_dir / 'config' / 'recursion'
                state = self.tmp / copy_dir / 'state'
                folder = config / 'plugins'
                manifest = fx.write_plugin(folder / real_dir, self.source, plugin_id='label.x', permissions=PERMS)
                fx.write_plugin(folder / copy_dir, self.source, plugin_id='label.x', permissions=PERMS)
                fx.write_plugin(folder / 'c-lone', self.source, plugin_id='label.lone', permissions=PERMS)
                for tampered in (copy_dir, 'c-lone'):
                    (folder / tampered / 'plugin.py').write_bytes(self.source + b'TAMPERED = True\n')
                    os.chmod(folder / tampered / 'plugin.py', 0o600)
                grants = [fx.grant_for(self.source, plugin_id=pid, permissions=PERMS)
                          for pid in ('label.x', 'label.lone')]
                fx.write_install(config, grants=grants, registry_labels={_digest(manifest): 'verified'})
                with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                    host = plugins.PluginHost(config, state, gil_probe=FakeGilProbe())
                self.addCleanup(host.close)
                control = adapter.DaemonControl(None, None, None, None, state, plugins=host)
                rows = [row for row in control.call('plugin.list')['plugins'] if row['plugin_id'] == 'label.x']
                self.assertEqual(sorted((row['state'], row['source']) for row in rows),
                                 [('loaded', str(folder / real_dir)), ('refused', str(folder / copy_dir))])
                loaded = next(row for row in rows if row['state'] == 'loaded')
                answer = control.call('plugin.status', {'plugin_id': 'label.x'})
                self.assertEqual(answer['code'], 'ok')
                self.assertEqual((answer['plugin']['state'], answer['plugin']['source']),
                                 ('loaded', str(folder / real_dir)))
                self.assertEqual(answer['plugin']['trust'], 'unsigned')
                self.assertEqual(answer['plugin']['registry_label'], 'verified')
                self.assertEqual(answer['plugin']['permissions'], ['read', 'panel'])
                self.assertEqual(answer['plugin']['permissions'], loaded['permissions'])
                lone = control.call('plugin.status', {'plugin_id': 'label.lone'})
                self.assertEqual((lone['code'], lone['plugin']['state'], lone['plugin']['code']),
                                 ('ok', 'refused', 'artifact_digest_mismatch'))

@unittest.skipUnless(LINUX, LINUX_ONLY)
@needs_crypto
class RunningDaemonLabelTests(unittest.TestCase):
    """One fixture, installed by hand, shows each label through plugin.status on a running daemon."""

    def test_each_label_shows_through_plugin_status_on_a_running_daemon(self):
        from recursion_core.daemon.tests.test_composition import ComposedDaemonTest
        case = ComposedDaemonTest('test_route_and_plugin_inventory_are_reads_of_the_running_daemon')
        case.setUp()
        try:
            config = Path(case.environ['XDG_CONFIG_HOME']) / 'recursion'
            source = fx.OPERATOR_SOURCE
            labels = {}
            expected = {}
            for plugin_id, label in (('label.verified', 'verified'), ('label.nonverified', 'non_verified'),
                                     ('label.plain', None)):
                manifest = fx.write_plugin(config / 'plugins' / plugin_id, source, plugin_id=plugin_id,
                                           permissions=PERMS)
                if label is not None:
                    labels[_digest(manifest)] = label
                expected[plugin_id] = label or 'unlisted'
            grants = [fx.grant_for(source, plugin_id=plugin_id, permissions=PERMS) for plugin_id in expected]
            fx.write_install(config, grants=grants, registry_labels=labels)
            fx.ensure_revocation_store(case.state)
            _, composed = case.boot(plugin_options={'gil_probe': FakeGilProbe()})
            api = case.api(composed)
            listed = {row['plugin_id']: row for row in api.call('plugin.list')['plugins']}
            self.assertEqual(set(listed), set(expected))
            for plugin_id, label in expected.items():
                status = api.call('plugin.status', {'plugin_id': plugin_id})
                self.assertEqual(status['code'], 'ok', status)
                self.assertEqual(status['plugin']['registry_label'], label)
                self.assertEqual(status['plugin']['trust'], 'unsigned')
                self.assertEqual(status['plugin']['state'], 'loaded')
                self.assertNotIn('order', status['plugin']['permissions'])
                self.assertEqual(listed[plugin_id]['registry_label'], label)
                self.assertEqual(listed[plugin_id]['permissions'], status['plugin']['permissions'])
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
