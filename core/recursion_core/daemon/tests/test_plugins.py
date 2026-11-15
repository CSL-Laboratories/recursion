"""The daemon's plugin host: plugins reach the daemon only through ``plugin_security.PluginLoader``.

PRODUCT.md d84cdd26 (CSLR-D-012) sections 4.1 and 4.3. Real files, the real loader and real Ed25519
signatures where the interpreter has ``cryptography``; the cases that need no signature run on
every interpreter, including the one that proves a missing ``cryptography`` refuses every plugin.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from recursion_core.daemon import breaker as breaker_module
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui.client import ACTIONS, o7_command
from recursion_core.plugin_security import canonical, parse
from recursion_core.plugin_security.signing import backend_available
from recursion_core.plugin_security.tests.support import FakeGilProbe, Key, needs_crypto

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = ('Linux-only: the plugin host checks file ownership by uid and keeps its stores under the daemon '
              'state directory; on Windows PluginHost refuses PlatformUnsupported (platform_unsupported), '
              'covered by WindowsRefusalTests in daemon/tests/test_windows_refusal.py')
READ = 'operator.positions.inspect'


def codes(record):
    return [finding['code'] for finding in record['findings']]


class HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-plugins-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config, self.state = self.tmp / 'config' / 'recursion', self.tmp / 'state'
        self.plugins = self.config / 'plugins'

    def host(self, **options):
        # SIMULATED GIL state (off) unless a test passes its own probe: these suites also run on GIL
        # builds, where the real probe would poison the core at the first load. The real probe is
        # covered by plugin_security/tests/test_runtime.py and by the daemon evidence run on 3.14t.
        options.setdefault('gil_probe', FakeGilProbe())
        host = plugins.PluginHost(self.config, self.state, **options)
        self.addCleanup(host.close)
        return host

    def read(self, host, command=READ):
        return host.operator(o7_command(command, 'gui', False))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class VerificationUnavailableTests(HostCase):
    """4c: without ``cryptography`` nothing is verified, so nothing is loaded. Needs no signature."""

    def install_unsigned(self, marker):
        source = f'open({str(marker)!r}, "w").close()\n'.encode() + fx.OPERATOR_SOURCE
        fx.write_install(self.config, grants=[fx.grant_for(source)])
        fx.write_plugin(self.plugins / 'fixture', source)
        return source

    def test_missing_cryptography_refuses_every_plugin_and_runs_none(self):
        marker = self.tmp / 'plugin-code-ran'
        self.install_unsigned(marker)
        fx.write_plugin(self.plugins / 'second', fx.OPERATOR_SOURCE, plugin_id='fixture.second')
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR') as logs:
            host = self.host(verification_available=lambda: False)
        self.assertFalse(marker.exists(), 'plugin code ran without verification')
        self.assertEqual([r['state'] for r in host.records], ['refused', 'refused'])
        for record in host.records:
            self.assertEqual(record['code'], 'plugin_verification_unavailable')
            self.assertIn('cryptography missing', record['detail'])
            self.assertIn('plugin_security/README.md', record['detail'])
            self.assertEqual(codes(record), ['plugin_verification_unavailable'])
        self.assertTrue(any('plugin_verification_unavailable' in line for line in logs.output))
        self.assertIsNone(host.loader)
        self.assertEqual(host.registries, ())
        inventory = host.inventory()
        self.assertEqual(inventory['verification'],
                         'unavailable: cryptography missing; install recipe in plugin_security/README.md')
        json.dumps(inventory)  # the inventory is plain JSON for the socket
        status, body = self.read(host)
        self.assertEqual(status, 503)
        self.assertEqual(body, {'error': 'unavailable: plugin_verification_unavailable: cryptography missing; '
                                         'install recipe in plugin_security/README.md'})
        rows = host.route_records()
        self.assertEqual([row['configured'] for row in rows], [False, False])
        self.assertTrue(all(row['blocker'].startswith('refused: plugin_verification_unavailable') for row in rows))

    def test_this_interpreter_never_loads_unverified(self):
        # No injection: what this interpreter really does. With cryptography the unsigned plugin loads
        # as unsigned (4.3); without it, it is refused. Either way the outcome is stated, never skipped.
        marker = self.tmp / 'plugin-code-ran'
        self.install_unsigned(marker)
        if backend_available():
            host = self.host()
            self.assertEqual((host.records[0]['state'], host.records[0]['trust']), ('loaded', 'unsigned'))
            self.assertTrue(marker.exists())
        else:
            with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                host = self.host()
            self.assertEqual(host.records[0]['code'], 'plugin_verification_unavailable')
            self.assertFalse(marker.exists())

    def test_nothing_installed_needs_no_cryptography_and_says_so(self):
        host = self.host(verification_available=lambda: False)
        self.assertEqual(host.records, [])
        self.assertEqual(self.read(host), (503, {'error': 'unavailable: no operator plugin is installed'}))


@unittest.skipUnless(LINUX, LINUX_ONLY)
@needs_crypto
class LoaderPathTests(HostCase):
    def setUp(self):
        super().setUp()
        self.key = Key()

    def install(self, source=fx.OPERATOR_SOURCE, *, permissions=('read', 'panel'), granted=('read', 'panel'), **plugin):
        fx.write_install(self.config, trusted={'dev': self.key.public},
                         grants=[fx.grant_for(source, key=self.key, permissions=granted)])
        return fx.write_plugin(self.plugins / 'fixture', source, key=self.key, permissions=permissions, **plugin)

    def test_signed_plugin_is_loaded_by_the_loader_and_serves_panels_and_reads(self):
        self.install()
        before = set(sys.modules)
        host = self.host()
        record, = host.records
        self.assertEqual((record['state'], record['plugin_id'], record['trust']), ('loaded', 'fixture.operator', 'signed'))
        self.assertEqual(record['permissions'], ['read', 'panel'])
        self.assertEqual(record['provides'], ['panels', 'operator_reads'])
        self.assertIn('signature_valid_trusted_key', codes(record))
        self.assertIn('active', codes(record))
        self.assertEqual(record['containment'], 'in process, same uid: not contained')
        # Loaded from bytes by the loader, never imported by name.
        self.assertEqual([m for m in set(sys.modules) - before if 'fixture' in m or m.startswith('csl_plugin')], [])
        self.assertEqual(host.loader.permissions('fixture.operator'), ('read', 'panel'))
        registry, = host.registries
        self.assertEqual([p['id'] for p in registry['panels']], ['fixture.positions', 'fixture.health'])
        self.assertEqual(registry['required'], {'venue_use': ('kalshi:fixture_read',)})
        status, body = self.read(host)
        self.assertEqual(status, 200)
        self.assertEqual(body['command'], READ)
        self.assertEqual(body['result']['positions'][0]['symbol'], 'FIXTURE-OWN-ROW')
        row, = host.route_records()
        self.assertEqual((row['id'], row['class'], row['configured']), ('plugin:fixture.operator', 'plugin', True))
        self.assertIn('loaded signed; permissions: read, panel', row['blocker'])
        self.assertIn('not contained', row['blocker'])

    def test_tampered_artifact_is_refused_typed_and_the_next_plugin_still_loads(self):
        self.install()
        (self.plugins / 'fixture' / 'plugin.py').write_bytes(fx.OPERATOR_SOURCE + b'TAMPERED = True\n')
        other = b'VALUE = 1\n'
        fx.write_plugin(self.plugins / 'other', other, key=self.key, plugin_id='fixture.other', permissions=('read',))
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR') as logs:
            host = self.host()
        refused, loaded = host.records
        self.assertEqual((refused['state'], refused['code'], refused['plugin_id']),
                         ('refused', 'artifact_digest_mismatch', 'fixture.operator'))
        self.assertEqual(refused['findings'][-1], {'stage': 'daemon', 'code': 'artifact_digest_mismatch',
                                                   'severity': 'refuse', 'detail': ''})
        self.assertTrue(any('the daemon runs without it' in line for line in logs.output))
        self.assertEqual((loaded['state'], loaded['plugin_id']), ('loaded', 'fixture.other'))
        self.assertEqual(host.registries, ())
        self.assertEqual(self.read(host), (503, {'error': 'unavailable: no loaded plugin serves the operator contract'}))
        self.assertIn('refused: artifact_digest_mismatch', host.route_records()[0]['blocker'])

    def test_malformed_manifest_and_missing_artifact_are_typed_refusals(self):
        self.install(manifest=b'{"not":"a manifest"}')
        fx.write_plugin(self.plugins / 'noartifact', b'X = 1\n', key=self.key, plugin_id='fixture.noartifact')
        os.unlink(self.plugins / 'noartifact' / 'plugin.py')
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual([(r['state'], r['code']) for r in host.records],
                         [('refused', 'manifest_fields'), ('refused', 'plugin_file_missing')])

    def test_a_bad_signature_loads_as_unsigned_and_the_signed_grant_gives_it_nothing(self):
        # 4.3: "A plugin that fails the check loads as unsigned", and the grant is bound to the signer.
        self.install(signature='00' * 64)
        host = self.host()
        record, = host.records
        self.assertEqual((record['state'], record['trust'], record['permissions']), ('loaded', 'unsigned', []))
        self.assertIn('signature_invalid', codes(record))
        self.assertIn('grant_signer_mismatch', codes(record))
        self.assertIn('panel_permission_required', codes(record))
        self.assertIn('read_permission_required', codes(record))
        self.assertEqual((record['provides'], host.registries), ([], ()))
        self.assertEqual(self.read(host)[0], 503)

    def test_what_a_plugin_offers_needs_its_permission(self):
        self.install(granted=('read',))
        host = self.host()
        record, = host.records
        self.assertEqual(record['permissions'], ['read'])
        self.assertEqual(record['provides'], ['operator_reads'])
        self.assertIn('panel_permission_required', codes(record))
        self.assertEqual(host.registries, ())

    def test_a_bad_registry_costs_the_plugin_its_panels_not_the_daemon(self):
        source = b'def registry():\n    return {"panels": [{"id": "bad", "title": "Bad", "reads": ["stop"]}]}\n'
        self.install(source)
        host = self.host()
        record, = host.records
        self.assertEqual((record['state'], record['provides'], host.registries), ('loaded', [], ()))
        self.assertIn('plugin_registry_invalid', codes(record))

    def test_files_another_user_could_write_are_refused(self):
        self.install()
        os.chmod(self.plugins / 'fixture' / 'plugin.py', 0o666)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual(host.records[0]['code'], 'plugin_file_untrusted')
        os.chmod(self.plugins / 'fixture' / 'plugin.py', 0o600)
        os.chmod(self.plugins / 'fixture', 0o777)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual(host.records[0]['code'], 'plugin_directory_untrusted')

    def test_an_install_file_that_cannot_be_trusted_refuses_every_plugin(self):
        self.install()
        (self.plugins / 'install.json').write_text('{"trusted_keys": {}, "surprise": 1}')
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual([(r['state'], r['code']) for r in host.records], [('refused', 'plugin_install_config_invalid')])

    def test_reads_are_typed_when_the_plugin_fails_and_interventions_are_not_wired(self):
        self.install()
        host = self.host()
        self.assertEqual(self.read(host, 'operator.health.inspect'),
                         (502, {'error': 'unavailable: operator plugin failed: RuntimeError'}))
        status, body = host.operator(o7_command(ACTIONS[0], 'gui', True))
        self.assertEqual(status, 503)
        self.assertTrue(body['error'].startswith('unavailable: operator interventions are not wired'))
        self.assertEqual(host.operator({'command': 'operator.orders.place'}), (400, {'error': 'command_not_in_operator_contract'}))
        self.assertEqual(host.operator('not a command')[0], 400)

    def test_a_plugin_call_that_turns_the_gil_on_poisons_the_core(self):
        self.install()
        # SIMULATED GIL state: off after the load, on after the plugin's registry() call.
        host = self.host(gil_probe=FakeGilProbe([False, True]))
        self.assertEqual(host.health.poisoned, 'gil_enabled_after_plugin_call')
        self.assertEqual(self.read(host), (503, {'error': 'unavailable: core_poisoned_restart_required'}))
        self.assertEqual(host.inventory()['plugins'][0]['permissions'], [])

    def test_the_plugin_gets_the_core_boundary_and_no_effect_is_authorized(self):
        self.install()
        stored, _ = breaker_module.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        host = self.host(breaker=stored)
        self.assertEqual(host.records[0]['provides'], ['panels', 'operator_reads', 'core_client'])
        call = host.loader._active['fixture.operator'].module.CORE[0]  # noqa: SLF001 - the client the plugin was handed

        def ask(op, **args):
            return parse(call(canonical({'protocol': 'csl-core/v0', 'op': op, 'args': args})))

        self.assertEqual(ask('plugin.permissions', plugin_id='fixture.operator')['result'], {'permissions': ['read', 'panel']})
        self.assertEqual(ask('run.fault', run_id='run-1')['refusal'], {'code': 'host_only'})
        effect = {'schema': 'csl-effect/vNext', 'kind': 'order.submit', 'plugin_id': 'fixture.operator', 'run_id': 'run-1',
                  'connection': 'subscription:login', 'currency': 'USD', 'amount_minor': 1, 'payload': {},
                  'scope': {'account': 'a', 'venue': 'v', 'contract_family': 'f', 'adapter': 'd', 'environment': 'paper'}}
        self.assertEqual(ask('effect.request', effect=effect)['refusal'], {'code': 'permission_required'})
        self.assertEqual(len(host.pending_approvals), 0)
        self.assertEqual(host.inventory()['authority_profile'], 'not configured')


class ReadmeClaimTests(unittest.TestCase):
    """Plugins run in the daemon's process and uid. No README in core/ may call that a sandbox."""

    def test_no_core_readme_calls_the_plugin_boundary_a_sandbox(self):
        core = Path(plugins.__file__).resolve().parents[2]
        readmes = sorted(core.rglob('README.md'))
        self.assertIn(core / 'recursion_core' / 'plugin_security' / 'README.md', readmes)
        claims = []
        for readme in readmes:
            for number, line in enumerate(readme.read_text(encoding='utf-8').splitlines(), 1):
                if 'sandbox' in line.lower() and 'not a sandbox' not in line.lower():
                    claims.append(f'{readme.relative_to(core).as_posix()}:{number}: {line.strip()}')
        self.assertEqual(claims, [])

    def test_the_readme_carries_the_refusal_and_the_interpreter_recipe(self):
        text = (Path(plugins.__file__).resolve().parents[1] / 'plugin_security' / 'README.md').read_text(encoding='utf-8')
        self.assertIn(plugins.VERIFICATION_UNAVAILABLE, text)
        self.assertIn('python3.14t -m venv ~/.local/lib/recursion/venv', text)
        self.assertIn('pip install --only-binary=:all: -r core/recursion_core/plugin_security/requirements.txt', text)
        self.assertIn('assert not sys._is_gil_enabled()', text)
        self.assertIn('**Not a sandbox.**', text)


if __name__ == '__main__':
    unittest.main()
