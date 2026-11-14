"""R4 findings 2 (host half), 3, 4 and 7 at the daemon's plugin host (PLUGIN-HOST PR 1).

Decisions quoted (Lucas, Nov 12 2026, 1:13 PM, "Yes, as recommended", CSL-DECISIONS.md):
D1 "The control socket and Stop come up before plugins load, so a plugin that hangs or is malformed
cannot keep Stop from starting." D2 "... If a plugin turns the GIL on later, the daemon keeps Stop and
reconciliation alive and refuses plugin work; the call that did it answers 503." D4 "An untrusted
plugin config folder refuses every plugin". D0 "take the stricter, fail-closed option that the
reviewer's test already encodes".

Plugins are unsigned with manual grants and ``verification_available=lambda: True`` (SIMULATED where
``cryptography`` is missing, as in test_adv2_plugins.py), so these run on every Linux interpreter.
The GIL state is SIMULATED (``FakeGilProbe``). Threads synchronise on events; timeouts are watchdogs
that end a broken run, never part of an assertion.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.daemon import breaker as daemon_breaker
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui.client import o7_command
from recursion_core.plugin_security.tests.support import FakeGilProbe

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: the plugin host checks file ownership by uid'
READ = 'operator.positions.inspect'
WATCHDOG_S = 60.0
EVERYTHING = ('read', 'panel', 'order')
POISONED = (503, {'error': 'unavailable: core_poisoned_restart_required'})
# Panels, a reader and the core hook, each recording that it ran (into sys._r4_host_calls).
OFFERS = b'''
import sys
CALLS = sys._r4_host_calls


def registry():
    CALLS.append("registry")
    return {"panels": [{"id": "r4.positions", "title": "R4 positions", "reads": ["operator.positions.inspect"]}]}


def operator(command):
    CALLS.append("operator")
    return {"api_version": "o7.operator.v0", "command": command["command"], "status_code": "ok", "result": {}}


def bind_core(call):
    CALLS.append("bind_core")
'''


def codes(record):
    return [f['code'] for f in record['findings']]


@unittest.skipUnless(LINUX, LINUX_ONLY)
class HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-r4-host-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config, self.state = self.tmp / 'config' / 'recursion', self.tmp / 'state'
        self.plugins = self.config / 'plugins'
        self.marker = self.tmp / 'plugin-code-ran'
        self.calls = []
        sys._r4_host_calls = self.calls
        self.addCleanup(lambda: delattr(sys, '_r4_host_calls'))

    def touch(self):
        return f'with open({str(self.marker)!r}, "a") as _f:\n    _f.write("x")\n'.encode()

    @staticmethod
    def grant(source, plugin_id='fixture.operator', permissions=('read', 'panel')):
        return fx.grant_for(source, plugin_id=plugin_id, permissions=permissions,
                            unsigned_order_acknowledged='order' in permissions)

    def install(self, *plugins_, config=None):
        """``plugins_``: (directory name, source, plugin id, permissions) each, all granted."""
        config = self.config if config is None else config
        fx.write_install(config, grants=[self.grant(src, pid, perms) for _, src, pid, perms in plugins_])
        for name, src, pid, perms in plugins_:
            fx.write_plugin(config / 'plugins' / name, src, plugin_id=pid, permissions=perms)

    def host(self, **options):
        options.setdefault('gil_probe', FakeGilProbe())
        options.setdefault('verification_available', lambda: True)
        host = plugins.PluginHost(self.config, self.state, **options)
        self.addCleanup(host.close)
        return host

    def breaker(self):
        stored, _ = daemon_breaker.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        return stored

    def read(self, host):
        return host.operator(o7_command(READ, 'gui', False))


class UntrustedInstallDirectoryTests(HostCase):
    """D4 and R4 finding 2's host half (fix-2's first test, adapted to unsigned grants)."""

    def extra_plugin_granted_elsewhere(self):
        source = self.touch() + OFFERS
        extra = self.tmp / 'extra'
        fx.write_plugin(extra, source, plugin_id='extra.one')
        elsewhere = self.tmp / 'elsewhere'
        # An install.json that WOULD grant the --plugin directory, if the host read it.
        fx.write_install(elsewhere, grants=[self.grant(source, 'extra.one')])
        return extra, elsewhere / 'plugins'

    def assert_everything_refused(self, host):
        self.assertEqual([(r['state'], r['code']) for r in host.records],
                         [('refused', 'plugin_directory_untrusted')] * 2)
        self.assertEqual(host.records[1]['detail'], plugins.CONFIG_UNTRUSTED)
        self.assertFalse(self.marker.exists(), 'a plugin ran under an install.json the host called untrusted')
        self.assertIsNone(host.loader)
        self.assertFalse((self.state / 'plugin-security').exists(), 'no store is opened when nothing can load')

    def test_a_linked_install_directory_refuses_every_plugin_and_is_not_read(self):
        extra, real = self.extra_plugin_granted_elsewhere()
        self.config.mkdir(parents=True, mode=0o700)
        os.symlink(real, self.plugins)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host(plugin_dirs=[extra])
        self.assert_everything_refused(host)

    def test_a_group_writable_install_directory_refuses_every_plugin_and_is_not_read(self):
        extra, real = self.extra_plugin_granted_elsewhere()
        self.config.mkdir(parents=True, mode=0o700)
        shutil.copytree(real, self.plugins)
        os.chmod(self.plugins, 0o770)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host(plugin_dirs=[extra])
        self.assert_everything_refused(host)

    def test_an_install_path_that_is_not_a_directory_refuses_every_plugin(self):
        extra, _ = self.extra_plugin_granted_elsewhere()
        self.config.mkdir(parents=True, mode=0o700)
        self.plugins.write_text('{}')
        os.chmod(self.plugins, 0o600)
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host(plugin_dirs=[extra])
        self.assert_everything_refused(host)

    def test_no_install_directory_at_all_leaves_a_plugin_dir_ungranted_and_unrun(self):
        extra, _ = self.extra_plugin_granted_elsewhere()
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host(plugin_dirs=[extra])
        self.assertEqual([(r['state'], r['code']) for r in host.records], [('refused', 'plugin_not_granted')])
        self.assertFalse(self.marker.exists())


class PoisonedCallTests(HostCase):
    """D2 and R4 finding 4: a plugin call that turns the GIL on gets nothing of what it offered used."""

    def test_a_registry_that_turns_the_gil_on_attaches_nothing_and_bind_core_is_not_called(self):
        self.install(('one', OFFERS, 'fixture.operator', EVERYTHING))
        # SIMULATED: off after the import, on after registry().
        host = self.host(breaker=self.breaker(), gil_probe=FakeGilProbe([False, True]))
        record, = host.records
        self.assertEqual(host.health.poisoned, 'gil_enabled_after_plugin_call')
        self.assertEqual(self.calls, ['registry'])
        self.assertEqual((record['state'], record['provides'], host.registries), ('loaded', [], ()))
        self.assertIn('core_poisoned_restart_required', codes(record))
        self.assertEqual(self.read(host), POISONED)
        self.assertEqual(self.calls, ['registry'], 'the reader of a poisoning plugin was called')

    def test_a_bind_core_that_turns_the_gil_on_attaches_nothing(self):
        self.install(('one', OFFERS, 'fixture.operator', EVERYTHING))
        # SIMULATED: off after the import and after registry(), on after bind_core().
        host = self.host(breaker=self.breaker(), gil_probe=FakeGilProbe([False, False, True]))
        record, = host.records
        self.assertEqual(self.calls, ['registry', 'bind_core'])
        self.assertEqual((record['provides'], host.registries), ([], ()))
        self.assertEqual(host._operators, {})
        self.assertEqual(self.read(host), POISONED)

    def test_the_read_that_turns_the_gil_on_answers_503_and_so_does_every_later_one(self):
        self.install(('one', OFFERS, 'fixture.operator', ('read',)))
        host = self.host(gil_probe=FakeGilProbe([False, True]))
        self.assertIsNone(host.health.poisoned)
        self.assertEqual(self.read(host), POISONED)
        self.assertEqual(self.calls, ['operator'])
        self.assertEqual(self.read(host), POISONED)
        self.assertEqual(self.calls, ['operator'])

    def test_a_poison_from_outside_a_plugin_call_during_attach_also_attaches_nothing(self):
        # The daemon's sweep poisons the core when the GIL is on (daemon/__main__.py); a plugin being
        # attached at that moment publishes nothing.
        host = None

        def poison_then_answer():
            host.health.poison('gil_enabled_during_daemon_run')
            return {'panels': []}
        source = b'import sys\ndef registry():\n    return sys._r4_registry()\n'
        sys._r4_registry = poison_then_answer
        self.addCleanup(lambda: delattr(sys, '_r4_registry'))
        self.install(('one', source, 'fixture.operator', ('read', 'panel')))
        host = self.host(load=False)
        host.load_installed()
        self.assertEqual((host.records[0]['provides'], host.registries), ([], ()))
        self.assertIn('gil_enabled_during_daemon_run: nothing this plugin offers is used',
                      [f['detail'] for f in host.records[0]['findings']])


class CoreClientAndReadLimitTests(HostCase):
    """R4 finding 7 (fix-7's two tests, adapted): bind_core() only with a grant, and the read limit."""

    def test_a_plugin_with_no_permission_never_runs_so_never_receives_the_core_hook(self):
        source = self.touch() + b'BOUND = []\ndef bind_core(call):\n    BOUND.append(call)\n'
        fx.write_plugin(self.plugins / 'ungranted', source, plugin_id='ungranted.plugin', permissions=())
        with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
            host = self.host(breaker=self.breaker())
        record, = host.records
        self.assertEqual((record['state'], record['code'], record['plugin_id']),
                         ('refused', 'plugin_not_granted', 'ungranted.plugin'))
        self.assertNotIn('provides', record)
        self.assertNotIn('ungranted.plugin', host.loader._active)
        self.assertFalse(self.marker.exists())

    def test_bind_core_needs_a_permission_the_core_client_serves(self):
        stored = self.breaker()
        self.install(('one', OFFERS, 'fixture.operator', ('read', 'panel')))
        host = self.host(breaker=stored)
        record, = host.records
        self.assertEqual(self.calls, ['registry'])
        self.assertEqual(record['provides'], ['panels', 'operator_reads'])
        self.assertIn('core_client_permission_required', codes(record))
        host.close()
        self.calls.clear()
        self.install(('one', OFFERS, 'fixture.operator', EVERYTHING))
        host = self.host(breaker=stored)
        self.assertEqual(self.calls, ['registry', 'bind_core'])
        self.assertEqual(host.records[0]['provides'], ['panels', 'operator_reads', 'core_client'])

    def test_a_file_growing_after_fstat_cannot_exceed_the_read_limit(self):
        path = self.tmp / 'growing.py'
        path.write_bytes(b'A' * 10)
        os.chmod(path, 0o600)
        real_fstat = os.fstat
        grew = []

        def stat_then_grow(fd):
            info = real_fstat(fd)
            # A real append after a real stat: the descriptor now sees eleven bytes while the
            # captured metadata still reports ten. No fake read result.
            if not grew:
                with path.open('ab') as handle:
                    handle.write(b'B')
                grew.append(True)
            return info

        with mock.patch.object(plugins.os, 'fstat', side_effect=stat_then_grow):
            with self.assertRaises(plugins.PluginRefused) as refused:
                plugins._read_owned(path, 10)
        self.assertEqual(refused.exception.code, 'plugin_file_too_large')
        self.assertEqual(path.read_bytes(), b'A' * 10 + b'B')
        self.assertEqual(plugins._read_owned(path, 11), b'A' * 10 + b'B')  # at the limit it still reads


class DeferredLoadingTests(HostCase):
    """R4 finding 3 and D1's host half: a host that loads later, while every read answers."""

    HANG = (b'import sys\n_in, _release = sys._r4_hang\n_in.set()\n_release.wait(%d)\n' % int(WATCHDOG_S)) + OFFERS

    def hang_hooks(self):
        entered, release = threading.Event(), threading.Event()
        sys._r4_hang = (entered, release)
        self.addCleanup(lambda: delattr(sys, '_r4_hang'))
        self.addCleanup(release.set)
        return entered, release

    def start(self, host, release):
        thread = host.start_loading()
        self.addCleanup(lambda: (release.set(), thread.join(WATCHDOG_S)))  # release first, then join
        return thread

    def test_a_host_built_without_loading_reads_nothing_until_it_loads(self):
        self.install(('one', self.touch() + OFFERS, 'fixture.operator', ('read', 'panel')))
        host = self.host(load=False)
        self.assertEqual((host.state, host.ready.is_set(), host.records, host.registries), ('loading', False, [], ()))
        self.assertFalse(self.marker.exists(), 'plugin code ran before load_installed()')
        self.assertIsNone(host.loader)
        self.assertEqual(self.read(host), (503, {'error': 'unavailable: plugins are still loading'}))
        self.assertEqual(host.inventory()['plugins'], [])
        host.load_installed()
        self.assertEqual((host.state, host.ready.is_set()), ('ready', True))
        self.assertEqual(host.records[0]['state'], 'loaded')
        self.assertEqual(len(host.registries), 1)
        self.assertEqual(self.read(host)[0], 200)
        with self.assertRaises(RuntimeError):
            host.load_installed()

    def test_every_read_answers_while_a_plugin_hangs_in_its_import(self):
        entered, release = self.hang_hooks()
        self.install(('a-hangs', self.HANG, 'fixture.hangs', ('read', 'panel')),
                     ('b-next', self.touch() + b'VALUE = 1\n', 'fixture.next', ('read',)))
        host = self.host(load=False)
        self.start(host, release)
        self.assertTrue(entered.wait(WATCHDOG_S))
        # The import is held: everything below answers from what is published, which is nothing yet.
        self.assertEqual(host.state, 'loading')
        self.assertEqual(host.loading_source, str(self.plugins / 'a-hangs'))
        self.assertEqual(host.inventory()['plugins'], [])
        self.assertEqual(host.route_records(), [])
        self.assertEqual(host.registries, ())
        self.assertEqual(self.read(host), (503, {'error': 'unavailable: plugins are still loading'}))
        self.assertFalse(self.marker.exists(), 'the next plugin ran before the one ahead of it finished')
        release.set()
        self.assertTrue(host.ready.wait(WATCHDOG_S))
        self.assertEqual([(r['plugin_id'], r['state']) for r in host.records],
                         [('fixture.hangs', 'loaded'), ('fixture.next', 'loaded')])
        self.assertTrue(self.marker.exists())
        self.assertIsNone(host.loading_source)
        self.assertEqual(self.read(host)[0], 200)

    def test_a_plugin_is_published_whole_with_its_panels_and_reader(self):
        entered, release = self.hang_hooks()
        first = b'import sys\nsys._r4_hang[0].set()\nsys._r4_hang[1].wait(%d)\n' % int(WATCHDOG_S)
        self.install(('a-ok', OFFERS, 'fixture.operator', ('read', 'panel')),
                     ('b-hangs', first, 'fixture.hangs', ('read',)))
        host = self.host(load=False)
        self.start(host, release)
        self.assertTrue(entered.wait(WATCHDOG_S))
        # The first plugin is published as a whole while the second is still importing.
        self.assertEqual([r['plugin_id'] for r in host.records], ['fixture.operator'])
        self.assertEqual(len(host.registries), 1)
        self.assertEqual(self.read(host)[0], 200)
        release.set()
        self.assertTrue(host.ready.wait(WATCHDOG_S))

    def test_closing_while_a_plugin_loads_loads_nothing_after_it(self):
        entered, release = self.hang_hooks()
        self.install(('a-hangs', self.HANG, 'fixture.hangs', ('read', 'panel')),
                     ('b-next', self.touch() + b'VALUE = 1\n', 'fixture.next', ('read',)))
        host = self.host(load=False)
        thread = self.start(host, release)
        self.assertTrue(entered.wait(WATCHDOG_S))
        host.close()
        release.set()
        thread.join(WATCHDOG_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual([r['plugin_id'] for r in host.records], ['fixture.hangs'])
        self.assertFalse(self.marker.exists(), 'a plugin was loaded after the host was closed')
        self.assertTrue(host.ready.is_set())


@unittest.skipUnless(LINUX, LINUX_ONLY)
class StopWhileAPluginHangsTests(HostCase):
    """D1 through STOP (fix-3's socket test, adapted: the hanging plugin is granted, so it really runs).

    An EXPECTED FAILURE until lane STOP applies decision D1's reorder in
    ``daemon/control_adapter.py`` (``compose_control`` builds the plugin host before
    ``server.start()``). STOP removes ``expectedFailure`` in the pull request that does it: an
    unexpected success fails the suite.
    """

    @unittest.expectedFailure
    def test_control_socket_stop_works_while_a_plugin_hangs_at_import(self):
        from recursion_core.control.client import SocketAPI
        from recursion_core.daemon import Daemon, Registry
        from recursion_core.daemon import control_adapter as adapter
        from recursion_core.daemon.tests.test_composition import BOOT, STANDIN, ProcessUnitManager, Units
        rt = self.tmp / 'rt'
        rt.mkdir(mode=0o700)
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir(mode=0o700)
        engine = bin_dir / 'claude'
        shutil.copyfile(STANDIN, engine)
        os.chmod(engine, 0o700)
        probe = subprocess.run([str(engine)], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                               env={'PATH': '/usr/bin:/bin'}, check=False).stdout
        if b'stand-in engine got no user message' not in probe:
            self.skipTest('temp directory cannot execute the stand-in engine')
        units = Units()
        self.addCleanup(lambda: [proc.poll() is None and (os.killpg(proc.pid, 9), proc.wait())
                                 for proc in units.procs.values()])
        environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                   'XDG_RUNTIME_DIR': str(rt), 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
        routes = {'claude_code': {
            'route': {'engine': 'claude_code', 'model': 'stand-in-model', 'api_key_helper': 'absent',
                      'connection': {'kind': 'subscription', 'id': 'stand-in-login'}},
            'deadline_s': 120.0, 'binary': str(engine)}}
        options = {'gil_probe': FakeGilProbe(), 'verification_available': lambda: True}

        d1 = Daemon(Registry(self.state), ProcessUnitManager(units), boot_id=lambda: BOOT)
        d1.sweep()
        c1 = adapter.compose_control(d1, self.state, environ=environ, routes=routes, plugin_options=options)
        sock_path = c1.server.path
        run_id = SocketAPI(sock_path, timeout_s=10).call(
            'run.start', {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 slow=1'})['run']['run_id']
        c1.close()
        d1.close()

        entered, release = threading.Event(), threading.Event()
        sys._r4_hang = (entered, release)
        self.addCleanup(lambda: delattr(sys, '_r4_hang'))
        self.addCleanup(release.set)
        self.install(('hangs', DeferredLoadingTests.HANG, 'fixture.hangs', ('read', 'panel')))

        d2 = Daemon(Registry(self.state), ProcessUnitManager(units), boot_id=lambda: BOOT)
        self.addCleanup(d2.close)
        d2.sweep()
        composed = []
        thread = threading.Thread(target=lambda: composed.append(
            adapter.compose_control(d2, self.state, environ=environ, routes=routes, plugin_options=options)))
        thread.start()
        try:
            self.assertTrue(entered.wait(WATCHDOG_S), 'the plugin never started importing')
            # The import is held. With D1's reorder the socket is already serving, so Stop answers now.
            stopped = SocketAPI(sock_path, timeout_s=10).call('run.stop', {'run_id': run_id})
            self.assertEqual(stopped['code'], 'stopped', stopped)
        finally:
            release.set()
            thread.join(WATCHDOG_S)
            if composed:
                composed[0].close()


if __name__ == '__main__':
    unittest.main()
