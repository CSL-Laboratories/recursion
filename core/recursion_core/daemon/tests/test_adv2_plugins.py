"""ADV2 red-team: the daemon's plugin host (``daemon/plugins.py``) and its inventory reads.

PRODUCT.md d84cdd26 (CSLR-D-012) 4.1 ("Plugins run inside the daemon's process. Checks the
developer sees ..."), 4.3 ("Grants are bound to the artifact digest and the signing key"; "A plugin
that fails the check loads as unsigned"). S34's claim under test: a plugin the loader refuses, or
that faults, costs the daemon that plugin, not the daemon.

Plugins here are unsigned and granted ``read``/``panel`` by the installer, so they load on every
interpreter. ``verification_available=lambda: True`` tells the host the signature backend exists
(true on the 3.14t venv; SIMULATED on python3.13, where an unsigned manifest needs no signature
check). The GIL probe is SIMULATED (``FakeGilProbe``) as in test_plugins.py. An ``expectedFailure``
is a bug on 08775416; a plain test is a hardening that held.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui.client import ACTIONS, o7_command
from recursion_core.plugin_security.tests.support import FakeGilProbe

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: the plugin host checks file ownership by uid'
READ = 'operator.positions.inspect'
CORE = Path(__file__).resolve().parents[3]
ANSWER = b'''
def operator(command):
    return {"api_version": "o7.operator.v0", "command": command["command"], "status_code": "ok", "result": {}}
'''


def codes(record):
    return [f['code'] for f in record['findings']]


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv2-plugins-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config, self.state = self.tmp / 'config' / 'recursion', self.tmp / 'state'
        self.plugins = self.config / 'plugins'
        self.marker = self.tmp / 'plugin-code-ran'

    def install(self, source, name='one', plugin_id='fixture.operator', **grant):
        fx.write_install(self.config, grants=[fx.grant_for(source, plugin_id=plugin_id, **grant)])
        fx.write_plugin(self.plugins / name, source, plugin_id=plugin_id)

    def host(self, **options):
        options.setdefault('gil_probe', FakeGilProbe())
        options.setdefault('verification_available', lambda: True)
        host = plugins.PluginHost(self.config, self.state, **options)
        self.addCleanup(host.close)
        return host

    def read(self, host, command=READ):
        return host.operator(o7_command(command, 'gui', False))

    def touch(self):
        return f'with open({str(self.marker)!r}, "a") as _f:\n    _f.write("x")\n'.encode()


class Adv2FaultingPluginTests(Adv2HostCase):
    """ADV2-4: plugin code that raises a BaseException that is not an Exception."""

    def test_control_systemexit_at_import_is_a_refusal(self):
        self.install(b'raise SystemExit(3)\n')
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual((host.records[0]['state'], host.records[0]['code']), ('refused', 'plugin_import_failed'))

    @unittest.expectedFailure  # ADV2-4: SystemExit from registry() escapes PluginHost; the daemon does not start
    def test_systemexit_in_registry_costs_the_plugin_its_panels_not_the_daemon(self):
        self.install(b'def registry():\n    raise SystemExit(3)\n')
        try:
            host = self.host()
        except BaseException as exc:  # noqa: BLE001 - the point of the test
            self.fail(f'PluginHost raised {type(exc).__name__}; compose_control and the daemon would not start')
        self.assertIn('plugin_registry_invalid', codes(host.records[0]))

    @unittest.expectedFailure  # ADV2-4: KeyboardInterrupt from bind_core() escapes PluginHost; the daemon does not start
    def test_keyboardinterrupt_in_bind_core_costs_the_plugin_its_client_not_the_daemon(self):
        from recursion_core.daemon import breaker as daemon_breaker
        stored, _ = daemon_breaker.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        self.install(b'def bind_core(call):\n    raise KeyboardInterrupt\n')
        try:
            host = self.host(breaker=stored)
        except BaseException as exc:  # noqa: BLE001
            self.fail(f'PluginHost raised {type(exc).__name__}')
        self.assertIn('plugin_bind_core_failed', codes(host.records[0]))

    @unittest.expectedFailure  # ADV2-4: SystemExit from operator() escapes PluginHost.operator into the GUI request
    def test_systemexit_in_operator_is_a_502(self):
        self.install(b'def operator(command):\n    raise SystemExit(4)\n')
        host = self.host()
        try:
            status, body = self.read(host)
        except BaseException as exc:  # noqa: BLE001
            self.fail(f'operator() raised {type(exc).__name__} out of the host')
        self.assertEqual(status, 502)

    def test_exception_and_bad_answers_from_operator_are_typed(self):
        for source, expected in (
                (b'def operator(c):\n    raise RuntimeError("x")\n', 'operator plugin failed: RuntimeError'),
                (b'def operator(c):\n    return {"x": object()}\n', 'operator plugin failed: TypeError'),
                (b'def operator(c):\n    d = {}\n    d["d"] = d\n    return d\n', 'operator plugin failed: ValueError'),
                (b'def operator(c):\n    d = []\n    for _ in range(100000):\n        d = [d]\n    return {"d": d}\n',
                 'operator plugin failed: RecursionError'),
                (b'def operator(c):\n    return ["not", "an", "object"]\n', 'answered a non-object')):
            with self.subTest(expected=expected):
                shutil.rmtree(self.config, True)
                self.install(source)
                status, body = self.read(self.host())
                self.assertEqual(status, 502)
                self.assertIn(expected, body['error'])

    def test_a_plugin_cannot_answer_an_intervention(self):
        self.install(b'CALLS = []\ndef operator(c):\n    CALLS.append(c)\n    return {"order_submitted": True}\n')
        host = self.host()
        for action in ACTIONS:
            status, body = host.operator(o7_command(action, 'gui', True))
            self.assertEqual(status, 503)
            self.assertIn('nothing was sent', body['error'])
        self.assertEqual(host.loader._active['fixture.operator'].module.CALLS, [])
        self.assertEqual(host.operator({'command': 'operator.orders.submit'}), (400, {'error': 'command_not_in_operator_contract'}))
        self.assertEqual(host.operator(['not', 'a', 'dict'])[0], 400)

    def test_gil_turned_on_by_operator_poisons_and_later_reads_refuse(self):
        self.install(ANSWER)
        probe = FakeGilProbe([False, True])  # off after the load, on after the first operator() call
        host = self.host(gil_probe=probe)
        first = self.read(host)
        # Observed on 08775416: the answer of the call that turned the GIL on is still served.
        self.assertEqual(first[0], 200)
        self.assertEqual(host.health.poisoned, 'gil_enabled_after_plugin_call')
        self.assertEqual(self.read(host), (503, {'error': 'unavailable: core_poisoned_restart_required'}))
        self.assertEqual(host.inventory()['plugins'][0]['permissions'], [])

    def test_gil_on_after_import_refuses_the_plugin_and_every_later_one(self):
        self.install(self.touch() + ANSWER)
        fx.write_plugin(self.plugins / 'two', ANSWER, plugin_id='fixture.two')
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host(gil_probe=FakeGilProbe([True]))
        self.assertEqual([r['code'] for r in host.records], ['core_poisoned_gil_enabled', 'core_poisoned_restart_required'])
        self.assertEqual(self.read(host)[0], 503)


class Adv2FilesystemTests(Adv2HostCase):
    def test_symlinked_plugin_directory_and_files_are_refused_before_any_code_runs(self):
        real = self.tmp / 'real'
        fx.write_plugin(real, self.touch() + ANSWER)
        fx.write_install(self.config, grants=[fx.grant_for(self.touch() + ANSWER)])
        os.symlink(real, self.plugins / 'linked-dir')
        linked_files = self.plugins / 'linked-files'
        linked_files.mkdir(mode=0o700)
        for name in ('manifest.json', 'plugin.py'):
            os.symlink(real / name, linked_files / name)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        by_source = {Path(r['source']).name: r['code'] for r in host.records}
        self.assertEqual(by_source, {'linked-dir': 'plugin_directory_untrusted', 'linked-files': 'plugin_file_unreadable'})
        self.assertFalse(self.marker.exists())

    def test_group_writable_files_and_install_are_refused(self):
        self.install(self.touch() + ANSWER)
        os.chmod(self.plugins / 'one' / 'plugin.py', 0o620)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual(host.records[0]['code'], 'plugin_file_untrusted')
        os.chmod(self.plugins / 'one' / 'plugin.py', 0o600)
        os.chmod(self.plugins / 'install.json', 0o666)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual(host.records[0]['code'], 'plugin_file_untrusted')
        self.assertFalse(self.marker.exists())

    def test_install_directory_that_is_group_writable_loads_nothing(self):
        self.install(self.touch() + ANSWER)
        os.chmod(self.plugins, 0o770)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual([r['code'] for r in host.records], ['plugin_directory_untrusted'])
        self.assertFalse(self.marker.exists())

    def test_artifact_swapped_on_disk_after_it_was_read_never_runs(self):
        good = ANSWER
        evil = self.touch() + ANSWER
        self.install(good)
        real_read = plugins._read_owned

        def read_then_swap(path, limit, **kw):
            data = real_read(path, limit, **kw)
            if Path(path).name == 'plugin.py':
                Path(path).write_bytes(evil)  # TOCTOU: the file changes after the host read it
            return data

        plugins._read_owned = read_then_swap
        self.addCleanup(setattr, plugins, '_read_owned', real_read)
        host = self.host()
        self.assertEqual(host.records[0]['state'], 'loaded')
        self.assertFalse(self.marker.exists(), 'bytes on disk ran instead of the bytes that were verified')

    def test_tampered_artifact_with_side_effects_never_runs(self):
        self.install(ANSWER)
        (self.plugins / 'one' / 'plugin.py').write_bytes(self.touch() + ANSWER)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        self.assertEqual(host.records[0]['code'], 'artifact_digest_mismatch')
        self.assertFalse(self.marker.exists())

    def test_manifest_with_path_traversal_or_huge_size_is_refused(self):
        src = self.touch() + ANSWER
        fx.write_install(self.config, grants=[fx.grant_for(src)])
        fx.write_plugin(self.plugins / 'trav', src, plugin_id='../../etc/evil')
        fx.write_plugin(self.plugins / 'huge', src, manifest=b'{' + b' ' * (plugins.MAX_MANIFEST_BYTES + 1) + b'}')
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host()
        by_source = {Path(r['source']).name: r['code'] for r in host.records}
        self.assertEqual(by_source['huge'], 'plugin_file_too_large')
        self.assertNotEqual(host.records[1]['state'], 'loaded')
        self.assertFalse(self.marker.exists())
        for row in host.route_records():
            self.assertNotIn('/', row['id'])

    def test_install_json_shapes_that_are_not_valid_refuse_every_plugin(self):
        for body in ('[]', '{"grants": {}}', '{"grants": [{"plugin_id": "a"}]}', '{"trusted_keys": {"k": "zz"}}',
                     '{"reserved": {"hosts": "x"}}', '{"authority_anchor": 5}', '{"extra": 1}', '\xff',
                     '{"grants": [{"plugin_id": "a", "artifact_sha256": "0", "permissions": [["read"]]}]}'):
            with self.subTest(body=body):
                self.install(self.touch() + ANSWER)
                (self.plugins / 'install.json').write_bytes(body.encode('latin-1'))
                with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                    host = self.host()
                self.assertEqual(host.records[0]['state'], 'refused')
                self.assertFalse(self.marker.exists())

    def test_a_grant_for_another_digest_gives_no_permission(self):
        # 4.3: grants are bound to the artifact digest. Same-uid tampering of install.json is the documented
        # same-uid limit; a grant that names another digest must not apply.
        source = ANSWER
        fx.write_install(self.config, grants=[fx.grant_for(b'other bytes')])
        fx.write_plugin(self.plugins / 'one', source)
        host = self.host()
        self.assertEqual(host.records[0]['state'], 'loaded')
        self.assertEqual(host.inventory()['plugins'][0]['permissions'], [])
        self.assertEqual(self.read(host)[0], 503)

    def test_duplicate_plugin_ids_load_once(self):
        self.install(self.touch() + ANSWER)
        fx.write_plugin(self.plugins / 'two', self.touch() + ANSWER)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host(plugin_dirs=[str(self.plugins / 'one')])
        self.assertEqual([r['state'] for r in host.records], ['loaded', 'refused', 'refused'])
        self.assertEqual([r['code'] for r in host.records][1:], ['plugin_already_active'] * 2)
        self.assertEqual(self.marker.read_text(), 'x', 'the duplicate ran plugin code')
        # Observed: three GUI rows share the id plugin:fixture.operator (one loaded, two refused).
        self.assertEqual({r['id'] for r in host.route_records()}, {'plugin:fixture.operator'})

    @unittest.expectedFailure  # ADV2-7: code a granted plugin imports from outside its verified artifact runs with no finding
    def test_code_imported_from_outside_the_artifact_is_a_finding(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'adv2_unverified_helper.py').write_text(f'with open({str(self.marker)!r}, "a") as f:\n    f.write("x")\n')
        sys.path.insert(0, str(outside))
        self.addCleanup(sys.path.remove, str(outside))
        self.addCleanup(sys.modules.pop, 'adv2_unverified_helper', None)
        self.install(b'import adv2_unverified_helper\n' + ANSWER)
        host = self.host()
        record = host.records[0]
        self.assertEqual(record['state'], 'loaded')
        self.assertTrue(self.marker.exists())  # unverified bytes ran under the grant bound to plugin.py's digest
        self.assertTrue(any('adv2_unverified_helper' in f.get('detail', '') for f in record['findings']),
                        f'no finding names the out-of-artifact module: {codes(record)}')


class Adv2CryptographyMissingTests(Adv2HostCase):
    def test_with_cryptography_really_unimportable_no_plugin_code_runs(self):
        # Not an injection into the host: cryptography is made unimportable before the daemon modules load.
        self.install(self.touch() + ANSWER)
        fx.write_plugin(self.tmp / 'extra', self.touch() + ANSWER, plugin_id='fixture.extra')
        code = ('import sys; sys.modules["cryptography"] = None\n'
                'from recursion_core.daemon import plugins\n'
                'from recursion_core.plugin_security.tests.support import FakeGilProbe\n'
                'from recursion_core.plugin_security.signing import backend_available\n'
                'assert not backend_available()\n'
                'h = plugins.PluginHost(sys.argv[1], sys.argv[2], plugin_dirs=[sys.argv[3]], gil_probe=FakeGilProbe())\n'
                'print(sorted({r["code"] for r in h.records}), h.loader, h.verification)\n')
        done = subprocess.run([sys.executable, '-B', '-c', code, str(self.config), str(self.state), str(self.tmp / 'extra')],
                              env={'PYTHONPATH': str(CORE)}, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("['plugin_verification_unavailable'] None unavailable: cryptography missing", done.stdout)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / 'plugin-security').exists(), 'no store is made when nothing can load')


class Adv2InventoryTests(Adv2HostCase):
    def control(self, host):
        return adapter.DaemonControl(None, None, None, None, self.state, routes={}, plugins=host)

    def test_route_and_plugin_lists_carry_no_key_token_or_secret(self):
        self.install(ANSWER)
        host = self.host()
        control = self.control(host)
        routes = json.dumps(control.call('route.list'))
        listed = json.dumps(control.call('plugin.list'))
        for text in (routes, listed):
            self.assertNotIn('BEGIN', text)
            self.assertNotIn(fx.FAKE_DEVELOPER_KEY, text)  # the developer public key is not echoed
        # Observed: plugin.list carries each plugin's absolute source path (owner-only socket);
        # the GUI's GET /v1/routes carries none.
        self.assertIn(str(self.plugins / 'one'), listed)
        gui = json.loads(control.gui_transport('GET', '/v1/routes', None)[1])
        self.assertNotIn(str(self.tmp), json.dumps(gui))

    def test_engine_peer_gets_no_route_or_plugin_list(self):
        host = self.host()
        control = self.control(host)

        class Conn:
            class peer:
                pid = -1  # no /proc entry: read as inside a run unit, fails closed
        for method in ('route.list', 'plugin.list'):
            answer = control.handle({'op': 'control', 'method': method, 'params': None}, Conn)
            self.assertEqual(answer['error']['code'], 'scope_refused')

    def test_many_refused_plugins_answer_a_typed_frame_error_not_a_crash(self):
        from recursion_core.control_transport.framing import FrameError, encode_frame
        host = self.host()
        host.records = [{'source': '/x/' + 'p' * 200 + str(i), 'plugin_id': None, 'state': 'refused',
                         'code': 'plugin_file_missing', 'detail': 'manifest.json', 'findings': []} for i in range(5000)]
        answer = self.control(host).call('plugin.list')
        with self.assertRaises(FrameError):  # the socket answers response_too_large (unix.py:471)
            encode_frame({'ok': True, 'result': answer})


if __name__ == '__main__':
    unittest.main()
