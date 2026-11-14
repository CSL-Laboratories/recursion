"""D2 at the daemon entry point (R4 finding 4, R1 finding 5; PLUGIN-HOST PR 1).

Decision D2 (Lucas, Nov 12 2026, 1:13 PM, "Yes, as recommended", CSL-DECISIONS.md): "The daemon refuses
to start without ``-X gil=0``. If a plugin turns the GIL on later, the daemon keeps Stop and
reconciliation alive and refuses plugin work; the call that did it answers 503."

These need CPython's GIL probe (``sys._is_gil_enabled``) and a free-threaded 3.14 build, as the daemon
itself does (``daemon/__init__.py``, ``require_free_threaded``): on the 3.12 that hosted CI runs they
are SKIPPED, and hosted CI does not run them. No probe is injected into the daemon; the sweep test
replaces ``sys._is_gil_enabled`` itself, which every GIL read in the process goes through.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.daemon.tests import plugin_fixture as fx

CORE = Path(__file__).resolve().parents[3]
FREE_THREADED_314 = (sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 14)
                     and bool(sysconfig.get_config_var('Py_GIL_DISABLED')))
LINUX = sys.platform.startswith('linux')
STARTED_WITH_X_GIL_0 = sys._xoptions.get('gil') == '0'
CHECK = ('from recursion_core.daemon.__main__ import require_free_threaded\n'
         'try:\n    require_free_threaded()\nexcept RuntimeError as exc:\n    print("refused", exc)\n'
         'else:\n    print("passed")\n')


def _probe(*flags, env_extra=None):
    env = {'PYTHONPATH': str(CORE), 'PATH': '/usr/bin:/bin', **(env_extra or {})}
    done = subprocess.run([sys.executable, *flags, '-B', '-c', CHECK], env=env, capture_output=True, text=True,
                          timeout=120, check=False)
    return done.returncode, done.stdout.strip(), done.stderr


@unittest.skipUnless(FREE_THREADED_314, 'needs free-threaded CPython 3.14 (the daemon refuses anything else)')
class StartupRequiresXGil0Tests(unittest.TestCase):
    def test_the_daemons_own_check_refuses_an_interpreter_started_without_X_gil_0(self):
        code, out, err = _probe()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, 'refused Requires free-threaded CPython 3.14t with -X gil=0 (runtime_requires_X_gil_0)')
        code, out, err = _probe('-X', 'gil=0')
        self.assertEqual((code, out), (0, 'passed'), err)

    def test_PYTHON_GIL_0_in_the_environment_does_not_stand_in_for_X_gil_0(self):
        # The decision's words are "-X gil=0"; the check reads sys._xoptions, which PYTHON_GIL does not set.
        code, out, err = _probe(env_extra={'PYTHON_GIL': '0'})
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('refused '), out)

    @unittest.skipUnless(LINUX, 'the daemon entry point is Linux-only')
    def test_the_entry_point_refuses_before_any_lock_state_socket_or_gui(self):
        from recursion_core.daemon import __main__ as entry

        def never(*args, **kwargs):
            raise AssertionError('the daemon went past the runtime refusal')
        names = ('check_operator_linger', 'UserLock', 'Registry', 'Daemon', 'compose_control', 'LoopbackGUI')
        saved = {name: getattr(entry, name) for name in names}
        self.addCleanup(lambda: [setattr(entry, name, value) for name, value in saved.items()])
        for name in names:
            setattr(entry, name, never)
        with mock.patch.dict(sys._xoptions, clear=True):  # as if started without -X gil=0
            with self.assertRaisesRegex(RuntimeError, 'runtime_requires_X_gil_0'):
                entry.main(port=0)
            with self.assertRaisesRegex(RuntimeError, 'runtime_requires_X_gil_0'):
                entry.main(client=object(), door=bool, port=0)


@unittest.skipUnless(hasattr(sys, '_is_gil_enabled'), 'requires the CPython GIL probe')
@unittest.skipUnless(FREE_THREADED_314 and STARTED_WITH_X_GIL_0 and LINUX,
                     'the real daemon entry point starts only on Linux free-threaded 3.14 run with -X gil=0')
class GilTurnedOnLaterTests(unittest.TestCase):
    """R1 fix-5 (review-R1 combination patch, hunk 14, and its test), written here as the brief says.

    The original test installed an ungranted plugin; an ungranted plugin no longer runs at all, so this
    one is granted. Its import turns the (replaced) GIL probe on. The loader poisons the core at once;
    then the sweep's runtime check fails on every tick. Without the fix that check's RuntimeError ended
    the daemon; with it the sweep, reconciliation and the control socket go on with plugins off.
    """

    def test_a_plugin_that_turns_the_gil_on_leaves_sweep_reconciliation_and_the_socket_running(self):
        from recursion_core.control.client import SocketAPI
        from recursion_core.daemon import __main__ as entry
        tmp = Path(tempfile.mkdtemp(prefix='rc-r4-gil-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chmod(tmp, 0o700)
        runtime = tmp / 'rt'
        runtime.mkdir(mode=0o700)
        bins = tmp / 'bin'  # read-only host probes: linger "yes", and an empty unit list
        bins.mkdir()
        (bins / 'loginctl').write_text('#!/bin/sh\n[ "$1" = show-user ] || exit 91\nprintf "yes\\n"\n')
        (bins / 'systemctl').write_text('#!/bin/sh\n[ "$1" = --user ] && [ "$2" = list-units ] || exit 92\n')
        for program in bins.iterdir():
            program.chmod(0o700)
        config = tmp / 'cfg' / 'recursion'
        source = b'import sys\nsys._r4_gil_on = True\n'
        fx.write_install(config, grants=[fx.grant_for(source, plugin_id='granted.gil', permissions=('read',))])
        fx.write_plugin(config / 'plugins' / 'gil', source, plugin_id='granted.gil', permissions=('read',))
        sys._r4_gil_on = False
        self.addCleanup(lambda: delattr(sys, '_r4_gil_on'))
        real_gil, real_event, real_compose = sys._is_gil_enabled, threading.Event, entry.compose_control
        seen, composed, ticks = [], [], [0]

        def compose(*args, **kwargs):
            built = real_compose(*args, **kwargs)
            reconcile = built.control.reconcile

            def counted():
                seen.append('reconcile')
                return reconcile()
            built.control.reconcile = counted
            composed.append(built)
            return built

        def make_event():
            event = real_event()
            if ticks[0] == 0:  # the first Event main() makes is its stop event
                ticks[0] = 1

                def wait(_timeout=None):
                    ticks[0] += 1
                    if ticks[0] == 3:  # the second sweep tick: the GIL has been on since the plugin loaded
                        api = SocketAPI(str(runtime / 'recursion' / 'control.sock'), timeout_s=10)
                        seen.append(('run.list', api.call('run.list').get('code')))
                        listed = api.call('plugin.list')
                        seen.append(('plugin.list', listed.get('code'), listed.get('core_poisoned')))
                    return ticks[0] > 3
                event.wait = wait
            return event

        env = {'XDG_RUNTIME_DIR': str(runtime), 'XDG_STATE_HOME': str(tmp / 'state'),
               'XDG_CONFIG_HOME': str(tmp / 'cfg'), 'RECURSION_ENGINE_USER': '', 'PATH': str(bins)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(entry, 'compose_control', side_effect=compose), \
                mock.patch.object(entry.threading, 'Event', side_effect=make_event), \
                mock.patch.object(sys, '_is_gil_enabled', side_effect=lambda: sys._r4_gil_on or real_gil()):
            entry.main(port=0, sweep_seconds=0.01)  # returns: it did not end on the runtime check
        self.assertEqual(ticks[0], 4)
        self.assertTrue(sys._r4_gil_on, 'the granted plugin did not run')
        host = composed[0].plugins
        self.assertEqual([(r['state'], r['code']) for r in host.records], [('refused', 'core_poisoned_gil_enabled')])
        self.assertEqual(host.health.poisoned, 'gil_enabled_after_load:granted.gil')
        # Reconciled at start and on both sweep ticks; the socket answered while the GIL was on.
        self.assertEqual(seen, ['reconcile', 'reconcile', ('run.list', 'ok'),
                                ('plugin.list', 'ok', 'gil_enabled_after_load:granted.gil'), 'reconcile'])


if __name__ == '__main__':
    unittest.main()
