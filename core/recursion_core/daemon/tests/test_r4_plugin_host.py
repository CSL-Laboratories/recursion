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
import types
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
        self.assertEqual((host.state, host.ready.is_set(), host.records, host.registries),
                         ('not_started', False, [], ()))
        self.assertFalse(self.marker.exists(), 'plugin code ran before load_installed()')
        self.assertIsNone(host.loader)
        self.assertEqual(self.read(host), (503, {'error': 'unavailable: plugin loading has not been started'}))
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


class ReviewOneTests(HostCase):
    """The regressions of review 1 of #380 (Codex GPT-6.1 Sol R1-R4, GPT-6 Astra R1-R3 and R5, follow-up F1).

    Adapted from the reviewers' own probes (`review-1-380-*-evidence/review_probes.py` and
    `test_review_invariants.py`); each failed on `94503d4c`.
    """

    def queued_read(self, poison_by_sweep):
        entered, release, queued = threading.Event(), threading.Event(), threading.Event()
        calls = []
        sys._r4_read = (entered, release, calls)
        self.addCleanup(lambda: delattr(sys, '_r4_read'))
        self.addCleanup(release.set)
        source = (b'import sys\ndef operator(command):\n    entered, release, calls = sys._r4_read\n'
                  b'    calls.append(command["command"])\n    if len(calls) == 1:\n        entered.set()\n'
                  b'        release.wait(%d)\n    return {"status_code": "ok", "result": {}}\n' % int(WATCHDOG_S))
        self.install(('one', source, 'fixture.operator', ('read',)))
        probe = FakeGilProbe([False]) if poison_by_sweep else FakeGilProbe([False, True])
        host = self.host(gil_probe=probe, operator_deadline_s=WATCHDOG_S, operator_turn_wait_s=WATCHDOG_S)
        real_wait = host._turn.wait_for

        def wait_for(*args, **kwargs):
            queued.set()
            return real_wait(*args, **kwargs)
        host._turn.wait_for = wait_for
        answers = []
        first = threading.Thread(target=lambda: answers.append(self.read(host)))
        second = threading.Thread(target=lambda: answers.append(self.read(host)))
        first.start()
        self.addCleanup(lambda: (release.set(), first.join(WATCHDOG_S)))
        self.assertTrue(entered.wait(WATCHDOG_S))
        second.start()
        self.addCleanup(lambda: (release.set(), second.join(WATCHDOG_S)))
        self.assertTrue(queued.wait(WATCHDOG_S))
        if poison_by_sweep:
            host.health.poison('gil_enabled_during_daemon_run')
        release.set()
        first.join(WATCHDOG_S)
        second.join(WATCHDOG_S)
        return host, calls, answers

    def test_a_queued_read_is_not_dispatched_after_the_read_before_it_poisons(self):  # Sol R1, Astra R2
        host, calls, answers = self.queued_read(poison_by_sweep=False)
        self.assertEqual(calls, [READ], 'a queued read ran plugin code after the core was poisoned')
        self.assertEqual(answers, [POISONED, POISONED])

    def test_a_queued_read_is_not_dispatched_after_the_sweep_poisons(self):  # Sol R1
        host, calls, answers = self.queued_read(poison_by_sweep=True)
        self.assertEqual(calls, [READ])
        self.assertEqual(answers, [POISONED, POISONED])

    def test_an_already_poisoned_core_calls_no_hook_at_all(self):  # Astra R2
        self.install(('one', OFFERS, 'fixture.operator', EVERYTHING))
        host = self.host(breaker=self.breaker(), load=False)
        host.health.poison('gil_enabled_during_daemon_run')
        host.load_installed()  # the loader refuses every load on a poisoned core
        self.assertEqual(self.calls, [])
        loaded = types.SimpleNamespace(plugin_id='fixture.operator', permissions=EVERYTHING,
                                       module=types.SimpleNamespace(registry=lambda: self.calls.append('registry')))
        record = {'provides': [], 'findings': []}
        self.assertEqual(host._attach(loaded, record), {})  # _attach itself, reached directly
        self.assertEqual(self.calls, [])
        self.assertIn('core_poisoned_restart_required', codes(record))

    def test_a_registry_answer_copied_after_the_call_is_still_guarded(self):  # Sol R2
        sys._r4_copy_gil = False
        self.addCleanup(lambda: delattr(sys, '_r4_copy_gil'))
        probe = types.SimpleNamespace(gil_enabled=lambda: sys._r4_copy_gil)
        source = (b'import sys\ndef panels():\n    sys._r4_copy_gil = True\n'
                  b'    yield {"id": "copy.panel", "title": "Copy", "reads": ["operator.positions.inspect"]}\n'
                  b'def registry():\n    return {"panels": panels()}\n')
        self.install(('one', source, 'fixture.operator', ('read', 'panel')))
        host = self.host(gil_probe=probe)
        self.assertTrue(sys._r4_copy_gil, 'the registry generator was not materialized')
        self.assertEqual(host.health.poisoned, 'gil_enabled_after_plugin_call')
        self.assertEqual((host.registries, host.records[0]['provides']), ((), []))

    def test_a_read_answer_copied_after_the_call_is_still_guarded(self):  # Sol R2
        sys._r4_copy_gil = False
        self.addCleanup(lambda: delattr(sys, '_r4_copy_gil'))
        probe = types.SimpleNamespace(gil_enabled=lambda: sys._r4_copy_gil)
        source = (b'import sys\nclass Answer(dict):\n    def items(self):\n        sys._r4_copy_gil = True\n'
                  b'        return super().items()\ndef operator(command):\n    return Answer(result={})\n')
        self.install(('one', source, 'fixture.operator', ('read',)))
        host = self.host(gil_probe=probe)
        self.assertEqual(self.read(host), POISONED)
        self.assertTrue(sys._r4_copy_gil, 'the JSON copy did not call the answer object')

    def test_bind_core_is_decided_on_the_live_permissions(self):  # Astra R1
        from recursion_core.plugin_security.signing import key_id
        self.install(('one', OFFERS, 'fixture.operator', EVERYTHING))
        host = self.host(breaker=self.breaker(), load=False)
        real_attach = host._attach

        def attach(loaded, record):
            # An ordinary key revocation lands while the plugin is being attached.
            self.assertEqual(host.loader.revoke_key(key_id(fx.FAKE_DEVELOPER_KEY)), ['fixture.operator'])
            return real_attach(loaded, record)
        host._attach = attach
        host.load_installed()
        record, = host.records
        self.assertEqual(host.loader.permissions('fixture.operator'), ('read', 'panel'))
        self.assertNotIn('bind_core', self.calls)
        self.assertEqual(record['provides'], ['panels', 'operator_reads'])
        self.assertIn('core_client_permission_required', codes(record))

    def test_close_while_the_trust_store_opens_leaves_no_store_open(self):  # Sol R3, Astra R3
        self.install(('one', b'VALUE = 1\n', 'fixture.operator', ('read',)))
        host = self.host(load=False)
        entered, release = threading.Event(), threading.Event()
        real_sources = host._sources

        def sources(*args):
            found = real_sources(*args)
            entered.set()
            release.wait(WATCHDOG_S)
            return found
        host._sources = sources
        thread = host.start_loading()
        self.addCleanup(lambda: (release.set(), thread.join(WATCHDOG_S)))
        self.assertTrue(entered.wait(WATCHDOG_S))
        host.close()
        opened = []
        real_trust = plugins.TrustStore

        def trust_store(*args, **kwargs):
            opened.append(real_trust(*args, **kwargs))
            return opened[-1]
        with mock.patch.object(plugins, 'TrustStore', side_effect=trust_store):
            release.set()
            thread.join(WATCHDOG_S)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(host._trust)
        self.assertEqual([store._db for store in opened], [None] * len(opened), 'a store was left open after close')
        self.assertEqual((host.records, host.ready.is_set()), ([], True))

    def test_close_while_the_ledger_opens_leaves_no_store_open(self):  # Astra R3, between the two stores
        anchor = '11' * 32
        fx.write_install(self.config, grants=[self.grant(b'VALUE = 1\n')], authority_anchor=anchor)
        fx.write_plugin(self.plugins / 'one', b'VALUE = 1\n', plugin_id='fixture.operator', permissions=('read', 'panel'))
        host = self.host(load=False)
        entered, release, ledger = threading.Event(), threading.Event(), mock.Mock()

        def authority(*args):
            entered.set()
            release.wait(WATCHDOG_S)
            return ledger
        with mock.patch.object(plugins, 'Authority', side_effect=authority):
            thread = host.start_loading()
            self.addCleanup(lambda: (release.set(), thread.join(WATCHDOG_S)))
            self.assertTrue(entered.wait(WATCHDOG_S))
            trust = host._trust
            host.close()
            self.assertIsNone(trust._db)  # the store owned before close() was closed by close()
            release.set()
            thread.join(WATCHDOG_S)
        self.assertEqual(ledger.close.call_count, 1, 'the ledger opened after close() was left open')
        self.assertIsNone(host.authority)
        self.assertEqual(host.records, [])

    def test_a_closed_host_loads_nothing_and_opens_no_store(self):  # Sol R3
        self.install(('one', self.touch() + b'VALUE = 1\n', 'fixture.operator', ('read',)))
        host = self.host(load=False)
        host.close()
        host.load_installed()
        self.assertEqual((host._trust, host.records, host.ready.is_set()), (None, [], True))
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / 'plugin-security').exists())

    def test_a_close_during_an_order_plugins_import_breaks_nothing(self):  # Sol's control, which held
        entered, release = threading.Event(), threading.Event()
        sys._r4_hang = (entered, release)
        self.addCleanup(lambda: delattr(sys, '_r4_hang'))
        self.addCleanup(release.set)
        self.install(('one', b'import sys\nsys._r4_hang[0].set()\nsys._r4_hang[1].wait(%d)\n' % int(WATCHDOG_S),
                      'fixture.operator', ('read', 'order')))
        host = self.host(load=False)
        errors = []
        worker = threading.Thread(target=lambda: errors.extend(_run_catching(host.load_installed)))
        worker.start()
        self.addCleanup(lambda: (release.set(), worker.join(WATCHDOG_S)))
        self.assertTrue(entered.wait(WATCHDOG_S))
        host.close()
        release.set()
        worker.join(WATCHDOG_S)
        self.assertEqual(errors, [])

    def test_a_directory_that_cannot_be_listed_is_a_typed_refusal(self):  # Astra F1
        host = self.host(load=False)
        with mock.patch.object(host, '_sources', side_effect=PermissionError('synthetic')):
            with self.assertLogs('recursion_core.daemon.plugins', 'ERROR'):
                host.load_installed()
        self.assertEqual([(r['state'], r['code'], r['detail']) for r in host.records],
                         [('refused', 'plugin_directory_unreadable', 'PermissionError')])
        self.assertTrue(host.ready.is_set())

    def test_every_read_during_publication_sees_one_whole_plugin(self):  # Sol R4, Astra R5
        # The writer is held at every line of _publish in turn; at each stop a reader checks that a
        # record advertising panels or reads comes with them, never one without the other.
        self.install(('a', OFFERS, 'fixture.operator', ('read', 'panel')))
        host = self.host(load=False)
        code = plugins.PluginHost._publish.__code__
        step, resume, done, seen = threading.Event(), threading.Event(), [], []

        def local(frame, event, arg):
            if event == 'line':
                step.set()
                resume.wait(WATCHDOG_S)
                resume.clear()
            return local

        def tracer(frame, event, arg):
            return local if event == 'call' and frame.f_code is code else None

        def run():
            sys.settrace(tracer)
            try:
                host.load_installed()
            finally:
                sys.settrace(None)
                done.append(True)
                step.set()
        worker = threading.Thread(target=run)
        worker.start()
        self.addCleanup(lambda: (done.append(True), resume.set(), worker.join(WATCHDOG_S)))
        while True:
            self.assertTrue(step.wait(WATCHDOG_S))
            step.clear()
            if done:
                break
            plugins_now = host.inventory()['plugins']
            panels = sum('panels' in r.get('provides', []) for r in plugins_now)
            readers = sum('operator_reads' in r.get('provides', []) for r in plugins_now)
            seen.append((panels, len(host.registries), readers, len(host._operators)))
            resume.set()
        worker.join(WATCHDOG_S)
        self.assertTrue(seen, 'the writer never stopped inside _publish')
        self.assertEqual([s for s in seen if (s[0], s[2]) != (s[1], s[3])], [], seen)
        self.assertEqual(len(host.registries), 1)


class ReviewOneDeepSeekTests(HostCase):
    """DeepSeek V4.1 Flash's break-it read of #380 (F1): a module ``__getattr__`` must not stop the host."""

    RAISING = (b'def __getattr__(name):\n    raise RuntimeError("boom from plugin")\n'
               b'def registry():\n    return {"panels": [{"id": "ga.panel", "title": "GA", '
               b'"reads": ["operator.positions.inspect"]}]}\n')

    def test_a_raising_module_getattr_costs_nothing_and_the_next_plugin_loads(self):
        self.install(('a', self.RAISING, 'fixture.getattr', ('read', 'panel')),
                     ('b', self.touch() + b'VALUE = 1\n', 'fixture.next', ('read',)))
        host = self.host(breaker=self.breaker())  # the constructor must not raise
        first, second = host.records
        self.assertEqual((first['plugin_id'], first['state'], first['provides']), ('fixture.getattr', 'loaded', ['panels']))
        self.assertEqual((second['state'], self.marker.exists()), ('loaded', True))
        self.assertEqual(len(host.registries), 1)

    def test_anything_escaping_attach_costs_only_that_plugin(self):
        self.install(('a', OFFERS, 'fixture.operator', ('read', 'panel')),
                     ('b', self.touch() + b'VALUE = 1\n', 'fixture.next', ('read',)))
        host = self.host(load=False)
        real_attach = host._attach

        def attach(loaded, record):
            if loaded.plugin_id == 'fixture.operator':
                raise RuntimeError('escaped')
            return real_attach(loaded, record)
        host._attach = attach
        host.load_installed()
        first, second = host.records
        self.assertEqual((first['state'], first['provides'], host.registries, host._operators), ('loaded', [], (), {}))
        self.assertIn('plugin_attach_failed', codes(first))
        self.assertEqual((second['state'], self.marker.exists()), ('loaded', True))

    def test_a_fifo_in_place_of_plugin_py_is_refused_without_waiting(self):  # F2
        self.install(('a', b'VALUE = 1\n', 'fixture.fifo', ('read',)),
                     ('b', self.touch() + b'VALUE = 1\n', 'fixture.next', ('read',)))
        fifo = self.plugins / 'a' / 'plugin.py'
        fifo.unlink()
        os.mkfifo(fifo, 0o600)
        host = self.host(load=False)
        thread = host.start_loading()

        def unblock():  # only for a broken run: a writer lets a blocked open return
            if thread.is_alive():  # also true for a thread that has set ready and is still returning
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                except OSError:  # ENXIO: no reader holds the FIFO open, so nothing is blocked on it
                    pass
                else:
                    os.close(fd)
            thread.join(WATCHDOG_S)
        self.addCleanup(unblock)
        self.assertTrue(host.ready.wait(WATCHDOG_S), 'the host waited on a FIFO')
        first, second = host.records
        self.assertEqual((first['state'], first['code']), ('refused', 'plugin_file_untrusted'))
        self.assertEqual((second['state'], self.marker.exists()), ('loaded', True))

    def test_a_shut_down_host_serves_no_plugin_call(self):  # F3
        self.install(('a', OFFERS, 'fixture.operator', ('read', 'panel')))
        host = self.host()
        host.close()
        self.assertEqual(self.read(host), (503, {'error': plugins.SHUT_DOWN}))
        self.assertNotIn('operator', self.calls)
        host.close()  # idempotent

    def test_a_plugin_refused_on_a_poisoned_core_carries_no_other_plugins_findings(self):  # F4
        self.install(('a', OFFERS, 'fixture.operator', ('read', 'panel')),
                     ('b', b'VALUE = 1\n', 'fixture.second', ('read',)),
                     ('c', b'VALUE = 2\n', 'fixture.third', ('read',)))
        host = self.host(gil_probe=FakeGilProbe([False, True]))  # the first plugin's registry() poisons
        first, second, third = host.records
        self.assertEqual(first['plugin_id'], 'fixture.operator')
        for record in (second, third):
            self.assertEqual((record['state'], record['code'], record['plugin_id']),
                             ('refused', 'core_poisoned_restart_required', None))
            self.assertNotIn('manifest_ok', codes(record))
        self.assertEqual(len({row['id'] for row in host.route_records()}), 3)

    def test_a_second_start_loading_starts_nothing_and_says_so(self):  # F5
        self.install(('a', b'VALUE = 1\n', 'fixture.operator', ('read',)))
        host = self.host(load=False)
        thread = host.start_loading()
        self.addCleanup(thread.join, WATCHDOG_S)
        self.assertIsNone(host.start_loading())
        with self.assertRaises(RuntimeError):
            host.load_installed()
        self.assertTrue(host.ready.wait(WATCHDOG_S))
        self.assertEqual([r['plugin_id'] for r in host.records], ['fixture.operator'])

    def test_the_refusal_code_is_exported_by_the_package(self):  # F6, its first half
        import recursion_core.plugin_security as package
        self.assertEqual(package.NOT_GRANTED, 'plugin_not_granted')
        self.assertIn('NOT_GRANTED', package.__all__)

    def test_bind_core_with_no_breaker_store_is_a_finding(self):  # DeepSeek, part 6, sentence 3
        self.install(('a', OFFERS, 'fixture.operator', EVERYTHING))
        host = self.host()  # no breaker store: there is no core client to hand over
        self.assertNotIn('bind_core', self.calls)
        self.assertIn('core_client_unavailable', codes(host.records[0]))

    def test_the_gil_is_read_after_an_escape_from_attach(self):
        self.install(('a', OFFERS, 'fixture.operator', ('read', 'panel')))
        host = self.host(load=False, gil_probe=FakeGilProbe([False, True]))  # off after the import, on after
        host._attach = lambda loaded, record: (_ for _ in ()).throw(RuntimeError('escaped'))
        host.load_installed()
        self.assertEqual(host.health.poisoned, 'gil_enabled_after_plugin_call')


def _pause_at(function, text):
    """A tracer that holds the calling thread at the line of ``function`` containing ``text``.

    Returns ``(tracer, entered, release)``: install ``tracer`` with ``sys.settrace`` in the thread to hold.
    """
    import inspect
    lines, start = inspect.getsourcelines(function)
    line = start + next(i for i, source in enumerate(lines) if text in source)
    code, entered, release = function.__code__, threading.Event(), threading.Event()

    def local(frame, event, arg):
        if event == 'line' and frame.f_lineno == line:
            entered.set()
            release.wait(WATCHDOG_S)
        return local

    def tracer(frame, event, arg):
        return local if event == 'call' and frame.f_code is code else None
    return tracer, entered, release


class ReviewTwoTests(HostCase):
    """The round-2 reads of #380 at d2eddb0e (Codex GPT-6 Astra C1-C3 and T1, GPT-6.1 Sol C1-C4)."""

    def nested(self, raises):
        sys._r4_nested_gil = False
        self.addCleanup(lambda: delattr(sys, '_r4_nested_gil'))
        probe = types.SimpleNamespace(gil_enabled=lambda: sys._r4_nested_gil)
        body = b'        raise ValueError("conversion")\n' if raises else b'        return super().__iter__()\n'
        source = (b'import sys\nclass Reads(list):\n    def __iter__(self):\n        sys._r4_nested_gil = True\n' + body +
                  b'def registry():\n    return {"panels": [{"id": "nested.panel", "title": "Nested", '
                  b'"reads": Reads(["operator.positions.inspect"])}]}\n')
        self.install(('a', source, 'fixture.operator', ('read', 'panel')))
        host = self.host(gil_probe=probe)
        record = host.records[0]
        self.assertEqual(host.registries, ())
        self.assertIn('plugin_registry_invalid', codes(record))
        # No plugin code after the last GIL read: either the nested value never ran, or the guard saw it.
        self.assertTrue(not sys._r4_nested_gil or host.health.poisoned, 'a nested callback ran after the guard')

    def test_a_nested_registry_value_runs_no_plugin_code_after_the_guard(self):  # both reads' C1
        self.nested(raises=False)

    def test_a_nested_registry_value_that_raises_runs_no_plugin_code_after_the_guard(self):  # both reads' C1
        self.nested(raises=True)

    def test_registry_values_are_built_in_types_exactly(self):  # C1: a str subclass is refused, plain data passes
        source = (b'class Title(str):\n    pass\n'
                  b'def registry():\n    return {"panels": [{"id": "sub.panel", "title": Title("T"), '
                  b'"reads": ["operator.positions.inspect"]}]}\n')
        self.install(('a', source, 'fixture.operator', ('read', 'panel')))
        host = self.host()
        self.assertEqual(host.registries, ())
        self.assertIn('plugin_registry_invalid', codes(host.records[0]))

    def test_a_read_queued_across_shutdown_is_not_dispatched(self):  # both reads' C2
        entered, release, queued = threading.Event(), threading.Event(), threading.Event()
        calls = []
        sys._r4_read = (entered, release, calls)
        self.addCleanup(lambda: delattr(sys, '_r4_read'))
        self.addCleanup(release.set)
        source = (b'import sys\ndef operator(command):\n    entered, release, calls = sys._r4_read\n'
                  b'    calls.append(1)\n    if len(calls) == 1:\n        entered.set()\n'
                  b'        release.wait(%d)\n    return {"status_code": "ok", "result": {}}\n' % int(WATCHDOG_S))
        self.install(('one', source, 'fixture.operator', ('read',)))
        host = self.host(operator_deadline_s=WATCHDOG_S, operator_turn_wait_s=WATCHDOG_S)
        real_wait = host._turn.wait_for

        def wait_for(*args, **kwargs):
            queued.set()
            return real_wait(*args, **kwargs)
        host._turn.wait_for = wait_for
        answers = []
        first = threading.Thread(target=lambda: answers.append(('first', self.read(host))))
        second = threading.Thread(target=lambda: answers.append(('second', self.read(host))))
        first.start()
        self.addCleanup(lambda: (release.set(), first.join(WATCHDOG_S)))
        self.assertTrue(entered.wait(WATCHDOG_S))
        second.start()
        self.addCleanup(lambda: (release.set(), second.join(WATCHDOG_S)))
        self.assertTrue(queued.wait(WATCHDOG_S))
        host.close()
        release.set()
        first.join(WATCHDOG_S)
        second.join(WATCHDOG_S)
        self.assertEqual(calls, [1], 'a read queued before the shutdown ran plugin code after it')
        self.assertEqual(sorted(answers), [('first', (503, {'error': plugins.SHUT_DOWN})),
                                           ('second', (503, {'error': plugins.SHUT_DOWN}))])

    def test_shutdown_waits_for_a_store_being_handed_to_the_host(self):  # Astra T1, Sol C4
        self.install(('a', b'VALUE = 1\n', 'fixture.operator', ('read',)))
        host = self.host(load=False)
        tracer, entered, release = _pause_at(plugins.PluginHost._own, 'setattr(self, attribute, store)')
        self.addCleanup(release.set)

        def load():
            sys.settrace(tracer)
            try:
                host.load_installed()
            finally:
                sys.settrace(None)
        loader = threading.Thread(target=load)
        loader.start()
        self.addCleanup(lambda: (release.set(), loader.join(WATCHDOG_S)))
        self.assertTrue(entered.wait(WATCHDOG_S))  # the trust store is open and being handed over
        shut = threading.Event()
        closer = threading.Thread(target=lambda: (host.close(), shut.set()))
        closer.start()
        self.addCleanup(lambda: (release.set(), closer.join(WATCHDOG_S)))
        self.assertFalse(shut.wait(0.5), 'shutdown returned while a store was being handed over')  # not yet
        release.set()
        self.assertTrue(shut.wait(WATCHDOG_S))
        loader.join(WATCHDOG_S)
        self.assertIsNone(host._trust._db, 'the handed-over store was left open after shutdown')

    def test_two_loads_started_at_once_admit_one(self):  # Sol C4
        self.install(('a', b'VALUE = 1\n', 'fixture.operator', ('read',)))
        host = self.host(load=False)
        tracer, entered, release = _pause_at(plugins.PluginHost._admit_load, 'self._load_started = True')
        self.addCleanup(release.set)

        def load():
            sys.settrace(tracer)
            try:
                host.load_installed()
            finally:
                sys.settrace(None)
        first = threading.Thread(target=load)
        first.start()
        self.addCleanup(lambda: (release.set(), first.join(WATCHDOG_S)))
        self.assertTrue(entered.wait(WATCHDOG_S))  # the first load has seen "not started" and not yet said so
        second, done = [], threading.Event()
        starter = threading.Thread(target=lambda: (second.append(host.start_loading()), done.set()))
        starter.start()
        self.addCleanup(lambda: (release.set(), starter.join(WATCHDOG_S)))
        self.assertFalse(done.wait(0.5), 'a second load was admitted while the first was being admitted')  # not yet
        release.set()
        self.assertTrue(done.wait(WATCHDOG_S))
        first.join(WATCHDOG_S)
        self.assertEqual(second, [None])
        self.assertEqual([r['plugin_id'] for r in host.records], ['fixture.operator'])


def _run_catching(call):
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - reported to the test thread
        return [exc]
    return []


@unittest.skipUnless(LINUX, LINUX_ONLY)
class StopWhileAPluginHangsTests(HostCase):
    """D1 through STOP (fix-3's socket test, adapted: the hanging plugin is granted, so it really runs).

    An EXPECTED FAILURE until lane STOP applies decision D1's reorder in
    ``daemon/control_adapter.py`` (``compose_control`` builds the plugin host before
    ``server.start()``; ``PluginHost(load=False)`` and ``load_installed()`` are what it needs).
    The reorder makes this an unexpected success, which fails the suite, so ``expectedFailure``
    comes off in step with it: this file is PLUGIN-HOST's, and the two lanes' order is the
    Liaison's to set (PLUGIN-HOST's questions.md).
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
