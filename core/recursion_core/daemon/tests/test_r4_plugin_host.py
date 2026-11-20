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
import time
import types
import unittest
from unittest import mock

from recursion_core.daemon import breaker as daemon_breaker
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.gui.client import o7_command
from recursion_core.plugin_security import loader
from recursion_core.plugin_security.loader import LoadedPlugin
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
        module = types.ModuleType('fixture_operator')  # a module, as the loader makes (review 3: hooks are read
        module.registry = lambda: self.calls.append('registry')  # through the module type's own slot)
        loaded = types.SimpleNamespace(plugin_id='fixture.operator', permissions=EVERYTHING, module=module)
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


def _recording_failure():
    """An exception class whose metaclass records every read of the class's attributes (plugin code)."""
    class Recording(type):
        def __getattribute__(cls, name):
            sys._r4_events.append('plugin code')
            return super().__getattribute__(name)

    class OrdinaryFailure(Exception, metaclass=Recording):
        pass
    return OrdinaryFailure


def _equal_to(target):
    """A metaclass whose classes say they equal ``target`` (plugin code, recorded)."""
    class Equal(type):
        def __eq__(cls, other):
            sys._r4_events.append('plugin code')
            return other is target
        __hash__ = type.__hash__
    return Equal


def _pairs(record):
    return [(f['code'], f['detail']) for f in record['findings']]


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ReviewThreeTests(HostCase):
    """The round-3 reads of #380 at 5c7e0ce5 (GPT-6 Astra 1 and its key follow-up; GPT-6.1 Sol 1 and 2).

    The SIMULATED GIL probe records each GIL read and the plugin-defined code records each call, in
    one list (``sys._r4_events``). No plugin code may follow the last GIL read; the names and scans the
    host makes after a plugin ran call no plugin code at all.
    """

    def setUp(self):
        super().setUp()
        self.events = []
        sys._r4_events = self.events
        self.addCleanup(lambda: delattr(sys, '_r4_events'))
        known = set(sys.modules)
        self.addCleanup(lambda: [sys.modules.pop(k, None) for k in list(sys.modules) if k not in known])

    def probe(self):
        return types.SimpleNamespace(gil_enabled=lambda: self.events.append('gil read') or False)

    def bare(self, permissions=('read', 'panel'), **hooks):
        """A host that has loaded nothing, and a loaded plugin whose module holds ``hooks``."""
        host = self.host(load=False, gil_probe=self.probe())
        module = types.ModuleType('r4_review_three')
        module.__dict__.update(hooks)
        return host, LoadedPlugin('fixture.operator', '0' * 64, 'unsigned', None, 'unlisted', permissions,
                                  module, (), '0' * 64)

    def attach(self, **hooks):
        host, loaded = self.bare(**hooks)
        record = {'findings': [], 'provides': []}
        return host._attach(loaded, record), record

    def assert_nothing_after_the_last_gil_read(self):
        calls = [i for i, event in enumerate(self.events) if event == 'plugin code']
        reads = [i for i, event in enumerate(self.events) if event == 'gil read']
        self.assertTrue(reads, 'the GIL was never read')
        self.assertFalse(calls and max(calls) > max(reads), f'plugin code after the last GIL read: {self.events}')

    def test_a_metaclass_equality_admits_no_str_subclass(self):  # Astra 1, Sol 1
        events = self.events

        class Title(str, metaclass=_equal_to(str)):
            def strip(self, *args):
                events.append('plugin code')
                return str.strip(self, *args)
        offer, record = self.attach(registry=lambda: {'panels': [{'id': 'meta.panel', 'title': Title('T'),
                                                                  'reads': [READ]}]})
        self.assertEqual(offer, {})
        self.assertIn(('plugin_registry_invalid', 'ValueError: panels not registered'), _pairs(record))
        self.assertNotIn('plugin code', self.events)

    def test_a_metaclass_equality_admits_no_list_subclass(self):  # Astra 1, Sol 1
        events = self.events

        class Reads(list, metaclass=_equal_to(list)):
            def __iter__(self):
                events.append('plugin code')
                return list.__iter__(self)
        offer, record = self.attach(registry=lambda: {'panels': [{'id': 'meta.panel', 'title': 'T',
                                                                  'reads': Reads([READ])}]})
        self.assertEqual(offer, {})
        self.assertIn('plugin_registry_invalid', codes(record))
        self.assertNotIn('plugin code', self.events)

    def test_a_loaded_plugin_with_a_metaclass_title_publishes_no_panel(self):  # Sol 1, through the loader
        source = (b'import sys\nclass Equal(type):\n    def __eq__(cls, other):\n'
                  b'        sys._r4_events.append("plugin code")\n        return other is str\n'
                  b'    __hash__ = type.__hash__\n'
                  b'class Title(str, metaclass=Equal):\n    def strip(self, *args):\n'
                  b'        sys._r4_events.append("plugin code")\n        return str.strip(self, *args)\n'
                  b'def registry():\n    return {"panels": [{"id": "meta.panel", "title": Title("T"), '
                  b'"reads": ["operator.positions.inspect"]}]}\n')
        self.install(('a', source, 'fixture.operator', ('read', 'panel')))
        host = self.host(gil_probe=self.probe())
        self.assertEqual(host.records[0]['state'], 'loaded')
        self.assertEqual(host.registries, ())
        self.assertIn('plugin_registry_invalid', codes(host.records[0]))
        self.assertNotIn('plugin code', self.events)

    def test_a_str_subclass_key_is_refused_and_never_hashed(self):  # Astra's and Sol's key follow-up
        events = self.events

        class Key(str):
            def __hash__(self):
                events.append('plugin code')
                return str.__hash__(self)
        panel = {Key('id'): 'key.panel', 'title': 'T', 'reads': [READ]}
        self.events.clear()  # building the panel above hashed the key once, in the test itself
        offer, record = self.attach(registry=lambda: {'panels': [panel]})
        self.assertEqual(offer, {})
        self.assertIn('plugin_registry_invalid', codes(record))
        self.assertNotIn('plugin code', self.events)

    def test_a_registry_failure_is_named_without_plugin_code(self):  # Sol 2, registry()
        failure = _recording_failure()

        def panels():
            raise failure('conversion')
            yield
        offer, record = self.attach(registry=lambda: {'panels': panels()})
        self.assertEqual(offer, {})
        self.assertIn(('plugin_registry_invalid', 'OrdinaryFailure: panels not registered'), _pairs(record))
        self.assertNotIn('plugin code', self.events)

    def test_a_bind_core_failure_is_named_without_plugin_code(self):  # Sol 2, bind_core()
        failure = _recording_failure()

        def bind_core(core):
            raise failure('binding')
        host, loaded = self.bare(permissions=('order',), bind_core=bind_core)
        host.loader, host.core = mock.Mock(), mock.Mock()
        host.loader.permissions.return_value = ('order',)
        record = {'findings': [], 'provides': []}
        host._attach(loaded, record)
        self.assertIn(('plugin_bind_core_failed', 'OrdinaryFailure'), _pairs(record))
        self.assertNotIn('plugin code', self.events)
        self.assert_nothing_after_the_last_gil_read()

    def test_a_failed_read_is_named_without_plugin_code(self):  # Sol 2, operator()
        failure = _recording_failure()

        class Answer(dict):
            def items(self):
                raise failure('conversion')
        host = self.host(load=False, gil_probe=self.probe())
        host.loader = mock.Mock()
        host.loader.permissions.return_value = ('read',)
        host._operators = {'fixture.operator': lambda command: Answer(result={})}
        self.assertEqual(self.read(host), (502, {'error': 'unavailable: operator plugin failed: OrdinaryFailure'}))
        self.assertNotIn('plugin code', self.events)

    def test_an_attach_failure_is_named_without_plugin_code(self):  # Sol 2, "the outer attachment diagnostics"
        failure = _recording_failure()

        def attach(host, loaded, record):
            raise failure('attach')
        self.install(('a', b'VALUE = 1\n', 'fixture.operator', ('read',)))
        with mock.patch.object(plugins.PluginHost, '_attach', new=attach):
            host = self.host(gil_probe=self.probe())
        self.assertIn(('plugin_attach_failed', 'OrdinaryFailure: nothing this plugin offers is used'),
                      _pairs(host.records[0]))
        self.assertNotIn('plugin code', self.events)

    def test_a_type_name_is_a_plain_str_copied_without_plugin_code(self):  # Sol 2: a name can be a str subclass
        events = self.events

        class Name(str):
            def __format__(self, spec):
                events.append('plugin code')
                return str.__format__(self, spec)

            def __str__(self):
                events.append('plugin code')
                return str.__str__(self)

        class Failure(Exception):
            pass
        Failure.__name__ = Name('Renamed')
        name = loader.type_name(Failure('x'))
        self.assertIs(type(name), str)
        self.assertEqual(f'{name}: detail', 'Renamed: detail')
        self.assertNotIn('plugin code', self.events)

    def test_an_import_failure_is_named_without_plugin_code_after_the_gil_read(self):  # Sol 2, the loader's import
        source = (b'import sys\nclass Recording(type):\n    def __getattribute__(cls, name):\n'
                  b'        sys._r4_events.append("plugin code")\n        return super().__getattribute__(name)\n'
                  b'class OrdinaryFailure(Exception, metaclass=Recording):\n    pass\n'
                  b'raise OrdinaryFailure("import")\n')
        self.install(('a', source, 'fixture.operator', ('read',)))
        host = self.host(gil_probe=self.probe())
        record = host.records[0]
        self.assertEqual((record['state'], record['code'], record['detail']),
                         ('refused', 'plugin_import_failed', 'OrdinaryFailure'))
        self.assert_nothing_after_the_last_gil_read()

    def test_an_odd_sys_modules_entry_neither_runs_nor_skips_the_gil_read(self):  # found probing Sol 2
        source = (b'import sys\nclass Odd:\n    def __getattribute__(self, name):\n'
                  b'        sys._r4_events.append("plugin code")\n        raise RuntimeError("odd")\n'
                  b'sys.modules["r4_odd_entry"] = Odd()\n'
                  b'import types\nodd_file = types.ModuleType("r4_odd_file")\nodd_file.__file__ = 5\n'
                  b'sys.modules["r4_odd_file"] = odd_file\n'
                  b'def operator(command):\n    return {"status_code": "ok", "result": {}}\n')
        self.install(('a', source, 'fixture.operator', ('read',)))
        host = self.host(gil_probe=self.probe())
        record = host.records[0]
        self.assertEqual((record['state'], record['provides']), ('loaded', ['operator_reads']))
        self.assertIn((plugins.UNVERIFIED_IMPORTS, 'r4_odd_entry,r4_odd_file'), _pairs(record))
        self.assertIn('gil read', self.events)
        self.assertNotIn('plugin code', self.events)

    def test_a_str_subclass_name_in_sys_modules_runs_no_plugin_code_after_the_gil_read(self):  # found probing
        # The name hashes like "sys", so any set or dict holding both compares them: set(sys.modules)
        # reuses stored hashes and calls nothing for a name that collides with no other.
        source = (b'import sys\nclass Name(str):\n    def __hash__(self):\n'
                  b'        sys._r4_events.append("plugin code")\n        return hash("sys")\n'
                  b'    def __eq__(self, other):\n        sys._r4_events.append("plugin code")\n'
                  b'        return str.__eq__(self, other)\n'
                  b'    def partition(self, sep):\n        sys._r4_events.append("plugin code")\n'
                  b'        return str.partition(self, sep)\n'
                  b'sys.modules[Name("r4_named_entry")] = sys\n'
                  b'def operator(command):\n    return {"status_code": "ok", "result": {}}\n')
        # Plugin b loads after a: the scans around b's import must not touch a's name either.
        self.install(('a', source, 'fixture.operator', ('read',)), ('b', b'VALUE = 1\n', 'fixture.other', ('read',)))
        host = self.host(gil_probe=self.probe())
        self.assertEqual([r['state'] for r in host.records], ['loaded', 'loaded'])
        self.assertNotIn(plugins.UNVERIFIED_IMPORTS, codes(host.records[0]))  # the name is not a plain str: not read
        calls = [i for i, event in enumerate(self.events) if event == 'plugin code']
        reads = [i for i, event in enumerate(self.events) if event == 'gil read']
        self.assertTrue(calls and len(reads) == 2, self.events)
        self.assertLess(max(calls), min(reads), f'plugin code after plugin a\'s own GIL read: {self.events}')

    def test_a_hook_is_read_without_comparing_the_plugins_keys(self):  # found probing: _hook after the guard
        events = self.events

        class Name(str):
            def __hash__(self):
                return hash('operator')

            def __eq__(self, other):
                events.append('plugin code')
                return False
        host, loaded = self.bare()
        loaded.module.__dict__[Name('decoy')] = None  # a key of the same hash as "operator", before it
        loaded.module.registry = lambda: {'panels': [{'id': 'hook.panel', 'title': 'T', 'reads': [READ]}]}
        loaded.module.operator = lambda command: {'status_code': 'ok', 'result': {}}
        self.events.clear()  # setting "operator" above compared it with the decoy once, in the test itself
        record = {'findings': [], 'provides': []}
        offer = host._attach(loaded, record)
        self.assertEqual(sorted(offer), ['operator', 'panels'])
        self.assertNotIn('plugin code', self.events)

    def test_a_module_entry_is_read_through_the_module_slot(self):  # found probing: a module class changed
        events = self.events

        class Module(types.ModuleType):
            def __getattribute__(self, name):
                events.append('plugin code')
                return super().__getattribute__(name)
        module = types.ModuleType('r4_changed_class')
        module.operator = len
        module.__class__ = Module
        self.events.clear()
        self.assertIs(loader.module_entry(module, 'operator'), len)
        self.assertNotIn('plugin code', self.events)

    def test_a_read_after_shutdown_answers_at_once_without_queueing(self):  # DeepSeek F3's early check
        # Round 2's dispatch check gives the same answer, so only this tells the early check is there:
        # without it a read after shutdown waits for its turn behind a read still in the plugin.
        entered, release = threading.Event(), threading.Event()
        sys._r4_read = (entered, release)
        self.addCleanup(lambda: delattr(sys, '_r4_read'))
        self.addCleanup(release.set)
        source = (b'import sys\ndef operator(command):\n    entered, release = sys._r4_read\n    entered.set()\n'
                  b'    release.wait(%d)\n    return {"status_code": "ok", "result": {}}\n' % int(WATCHDOG_S))
        self.install(('one', source, 'fixture.operator', ('read',)))
        host = self.host(operator_deadline_s=WATCHDOG_S, operator_turn_wait_s=WATCHDOG_S)
        first = threading.Thread(target=lambda: self.read(host))
        first.start()
        self.addCleanup(lambda: (release.set(), first.join(WATCHDOG_S)))
        self.assertTrue(entered.wait(WATCHDOG_S))  # a read is in the plugin: the next one would queue for its turn
        host.close()
        queued = []
        host._turn.wait_for = lambda *args, **kwargs: queued.append(1) or False  # never waits, only records
        self.assertEqual(self.read(host), (503, {'error': plugins.SHUT_DOWN}))
        self.assertEqual(queued, [], 'a read after shutdown queued for its turn')

    def test_the_gil_is_read_even_if_the_extension_scan_fails(self):  # the loader's GIL read is in a finally
        source = b'import sys, types\nsys.modules["r4_fresh_entry"] = types.ModuleType("r4_fresh_entry")\n'
        self.install(('a', source, 'fixture.operator', ('read',)))
        host = self.host(load=False, gil_probe=self.probe())
        with mock.patch.object(loader, '_module_file', side_effect=RuntimeError('scan')):
            with self.assertRaises(RuntimeError):  # a fault of the host's own, injected: it is not hidden
                host.load_installed()
        self.assertEqual(self.events, ['gil read'])

    def test_a_namespace_changing_while_it_is_scanned_costs_only_the_finding(self):
        source = (b'import sys, types\nsys.modules["r4_fresh_entry"] = types.ModuleType("r4_fresh_entry")\n'
                  b'def operator(command):\n    return {"status_code": "ok", "result": {}}\n')
        self.install(('a', source, 'fixture.operator', ('read',)))
        with mock.patch.object(loader, 'module_entry', side_effect=RuntimeError('changed meanwhile')):
            host = self.host(gil_probe=self.probe())
        self.assertEqual((host.records[0]['state'], host.records[0]['provides']), ('loaded', ['operator_reads']))

    def test_sys_modules_is_read_again_while_other_threads_import(self):
        with mock.patch.object(loader, 'plain_items', side_effect=[RuntimeError(), RuntimeError(), [('a', 1)]]):
            self.assertEqual(loader.plain_modules(), {'a': 1})
        with mock.patch.object(loader, 'plain_items', side_effect=RuntimeError()):
            self.assertEqual(loader.plain_modules(), {})
        with mock.patch.object(loader, 'sys', types.SimpleNamespace(modules=object())):
            self.assertEqual(loader.plain_modules(), {})


def _run_catching(call):
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - reported to the test thread
        return [exc]
    return []


@unittest.skipUnless(LINUX, LINUX_ONLY)
class StopWhileAPluginHangsTests(HostCase):
    """The socket is serving while a granted plugin is held in its import, so Stop answers ``stopped``.

    The hanging plugin is granted, so it really runs.
    """

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


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RoundFiveAnswerTests(HostCase):
    """Round 5 of #380: the load-failure cleanup, and a call that passes its deadline."""

    def test_a_load_that_raises_shuts_the_socket_and_drops_the_device_token(self):
        """Removing the cleanup after ``load_installed`` leaves the socket and the device token."""
        from recursion_core.control.client import SocketAPI
        from recursion_core.daemon import Daemon, Registry
        from recursion_core.daemon import control_adapter as adapter
        from recursion_core.daemon.tests.test_composition import BOOT, ProcessUnitManager, Units

        runtime = self.tmp / 'rt'
        runtime.mkdir(mode=0o700)
        environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                   'XDG_RUNTIME_DIR': str(runtime), 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
        options = {'gil_probe': FakeGilProbe(), 'verification_available': lambda: True}
        units = Units()
        sock = runtime / 'recursion' / 'control.sock'
        token = runtime / 'recursion' / 'gui-device-token'
        daemon = Daemon(Registry(self.state), ProcessUnitManager(units), boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        daemon.sweep()
        composed = None
        try:
            with mock.patch.object(plugins.PluginHost, 'load_installed',
                                   side_effect=RuntimeError('load blew up')):
                with self.assertRaises(RuntimeError) as caught:
                    composed = adapter.compose_control(
                        daemon, self.state, environ=environ, routes={}, plugin_options=options)
            self.assertEqual(str(caught.exception), 'load blew up')
        finally:
            if composed is not None:
                composed.close()
        self.assertFalse(sock.exists(), 'a failed load left the control socket')
        self.assertFalse(token.exists(), 'a failed load left the device token')
        daemon.close()
        daemon2 = Daemon(Registry(self.state), ProcessUnitManager(units), boot_id=lambda: BOOT)
        self.addCleanup(daemon2.close)
        second = adapter.compose_control(
            daemon2, self.state, environ=environ, routes={}, plugin_options=options)
        self.addCleanup(second.close)
        answer = SocketAPI(second.server.path, timeout_s=10).call('plugin.list')
        self.assertEqual(answer['code'], 'ok', answer)

    def test_a_call_past_its_deadline_answers_504_and_later_reads_answer_503(self):
        """The call that is still out when the GIL is read answers 504; the next read answers 503."""
        entered, release = threading.Event(), threading.Event()
        sys._r4_deadline = (entered, release)
        self.addCleanup(lambda: delattr(sys, '_r4_deadline'))
        self.addCleanup(release.set)
        source = (b'import sys\ndef operator(command):\n    entered, release = sys._r4_deadline\n'
                  b'    entered.set()\n    release.wait(%d)\n'
                  b'    return {"status_code": "ok", "result": {}}\n' % int(WATCHDOG_S))
        self.install(('slow', source, 'fixture.operator', ('read',)))
        host = self.host(gil_probe=FakeGilProbe([False, True]), operator_deadline_s=0.2,
                         operator_turn_wait_s=0.2)
        with self.assertLogs('recursion_core.daemon.plugins', 'WARNING'):
            first = self.read(host)
            self.assertTrue(entered.is_set(), 'the plugin call never started')
            self.assertEqual(first, (504, {'error': plugins.OPERATOR_TIMED_OUT}))
            self.assertIsNone(host.health.poisoned, 'the poison landed before the timed-out call returned')
            release.set()
            limit = time.monotonic() + WATCHDOG_S
            while host.health.poisoned is None and time.monotonic() < limit:
                time.sleep(0.01)
            self.assertEqual(host.health.poisoned, 'gil_enabled_after_plugin_call')
            self.assertEqual(self.read(host), POISONED)


if __name__ == '__main__':
    unittest.main()
