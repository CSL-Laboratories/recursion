"""Round 5 of #380, the two lines left on the control adapter.

F4: a ``run.start`` served while ``load_installed`` has not returned is launched, and a later
failure of that load shuts the socket and leaves the unit. The socket admits the peer without
the device token, so delaying ``door.publish`` does not refuse the start.

F5: ``plugin_options={'load': ...}`` meets the ``load`` keyword ``compose_control`` already
passes and raises ``TypeError``.
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

from recursion_core.control.client import SocketAPI, socket_path
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import plugins
from recursion_core.daemon.tests.test_composition import BOOT, STANDIN, ProcessUnitManager, Units
from recursion_core.plugin_security.tests.support import FakeGilProbe

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: compose_control serves an AF_UNIX socket with peer credentials'
WATCHDOG_S = 30.0
START = {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 slow=1'}


def _kill_units(units):
    for proc in list(units.procs.values()):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ComposeWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc5w-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.runtime = self.tmp / 'rt'
        self.runtime.mkdir(mode=0o700)
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir(mode=0o700)
        self.engine = bin_dir / 'claude'
        shutil.copyfile(STANDIN, self.engine)
        os.chmod(self.engine, 0o700)
        probe = subprocess.run([str(self.engine)], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                               env={'PATH': '/usr/bin:/bin'}, check=False).stdout
        if b'stand-in engine got no user message' not in probe:
            self.skipTest('temp directory cannot execute the stand-in engine')
        self.units = Units()
        self.addCleanup(_kill_units, self.units)
        self.environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                        'XDG_RUNTIME_DIR': str(self.runtime), 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
        self.routes = {'claude_code': {
            'route': {'engine': 'claude_code', 'model': 'stand-in-model', 'api_key_helper': 'absent',
                      'connection': {'kind': 'subscription', 'id': 'stand-in-login'}},
            'deadline_s': 120.0, 'binary': str(self.engine)}}
        self.options = {'gil_probe': FakeGilProbe(), 'verification_available': lambda: True}
        self.sock = socket_path(self.environ)

    def boot(self):
        daemon = Daemon(Registry(self.state), ProcessUnitManager(self.units), boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        daemon.sweep()
        return daemon

    def compose(self, daemon, *, routes=None, plugin_options=None):
        return adapter.compose_control(daemon, self.state, environ=self.environ,
                                       routes=self.routes if routes is None else routes,
                                       plugin_options=self.options if plugin_options is None else plugin_options)

    def hold_load(self, after):
        """Replace ``load_installed`` with a hold, then ``after(host, real)`` once released."""
        entered, release = threading.Event(), threading.Event()
        real = plugins.PluginHost.load_installed

        def held(self_host):
            entered.set()
            if not release.wait(WATCHDOG_S):
                raise RuntimeError('the test never released the load')
            return after(self_host, real)

        return entered, release, held

    def wait_until_loading(self, entered):
        self.assertTrue(entered.wait(WATCHDOG_S), 'load_installed never started')
        self.assertTrue(os.path.exists(self.sock), 'the socket was not serving while plugins loaded')

    def compose_while_held(self, held, *, plugin_options=None):
        daemon = self.boot()
        box = {}

        def run():
            try:
                box['composed'] = self.compose(daemon, plugin_options=plugin_options)
            except BaseException as exc:  # noqa: BLE001 - the compose thread reports it to the test
                box['raised'] = exc

        thread = threading.Thread(target=run)
        thread.start()
        return box, thread

    def test_a_start_during_load_is_refused_and_a_failed_load_launches_nothing(self):
        """Two starts during the load are refused; reads and Stop still answer; a failed load leaves no unit."""
        def after(self_host, real):
            raise RuntimeError('load blew up')

        entered, release, held = self.hold_load(after)
        with mock.patch.object(plugins.PluginHost, 'load_installed', held):
            box, thread = self.compose_while_held(held)
            try:
                self.wait_until_loading(entered)
                api = SocketAPI(self.sock, timeout_s=10)
                self.assertEqual(api.call('plugin.list')['code'], 'ok')
                stop = api.call('run.stop', {'run_id': 'run-' + ('a' * 32)})
                self.assertNotEqual(stop.get('detail'), 'composing', stop)
                answers = []

                def once():
                    answers.append(api.call('run.start', START))

                first, second = threading.Thread(target=once), threading.Thread(target=once)
                first.start()
                second.start()
                first.join(WATCHDOG_S)
                second.join(WATCHDOG_S)
                self.assertEqual(len(answers), 2, answers)
                for answer in answers:
                    self.assertEqual((answer.get('code'), answer.get('detail')),
                                     ('unavailable', 'composing'), answer)
                self.assertEqual(self.units.started, [])
            finally:
                release.set()
                thread.join(WATCHDOG_S)
        self.assertFalse(thread.is_alive(), 'compose did not return')
        self.assertIsInstance(box.get('raised'), RuntimeError)
        self.assertEqual(str(box['raised']), 'load blew up')
        self.assertFalse(os.path.exists(self.sock), 'a failed load left the control socket')
        self.assertEqual(self.units.started, [])
        composed = box.get('composed')
        if composed is not None:
            composed.close()

    def test_a_start_works_once_the_load_has_returned(self):
        """The refusal lasts only until ``load_installed`` returns; a later start is launched."""
        def after(self_host, real):
            return real(self_host)

        entered, release, held = self.hold_load(after)
        with mock.patch.object(plugins.PluginHost, 'load_installed', held):
            box, thread = self.compose_while_held(held)
            try:
                self.wait_until_loading(entered)
                during = SocketAPI(self.sock, timeout_s=10).call('run.start', START)
                self.assertEqual((during.get('code'), during.get('detail')),
                                 ('unavailable', 'composing'), during)
                self.assertEqual(self.units.started, [])
            finally:
                release.set()
                thread.join(WATCHDOG_S)
        self.assertFalse(thread.is_alive(), 'compose did not return')
        self.assertNotIn('raised', box, box.get('raised'))
        composed = box['composed']
        self.addCleanup(composed.close)
        self.assertEqual(composed.plugins.state, 'ready')
        started = SocketAPI(self.sock, timeout_s=20).call('run.start', START)
        self.assertEqual(started.get('code'), 'started', started)
        self.assertEqual(len(self.units.started), 1)

    def test_an_interrupted_load_refuses_the_start_and_shuts_the_socket(self):
        """``KeyboardInterrupt`` during the load is the same window: no unit, and the socket is shut."""
        def after(self_host, real):
            raise KeyboardInterrupt

        entered, release, held = self.hold_load(after)
        with mock.patch.object(plugins.PluginHost, 'load_installed', held):
            box, thread = self.compose_while_held(held)
            try:
                self.wait_until_loading(entered)
                during = SocketAPI(self.sock, timeout_s=10).call('run.start', START)
                self.assertEqual((during.get('code'), during.get('detail')),
                                 ('unavailable', 'composing'), during)
            finally:
                release.set()
                thread.join(WATCHDOG_S)
        self.assertFalse(thread.is_alive(), 'compose did not return')
        self.assertIsInstance(box.get('raised'), KeyboardInterrupt)
        self.assertFalse(os.path.exists(self.sock))
        self.assertEqual(self.units.started, [])

    def test_plugin_options_load_is_passed_to_the_host(self):
        """``load`` in ``plugin_options`` is the host's keyword, and the caller's mapping is not changed."""
        real = plugins.PluginHost.load_installed

        def spy(self_host, seen):
            seen['socket'] = os.path.exists(self.sock)
            return real(self_host)

        caller_true = {'load': True, 'gil_probe': FakeGilProbe(), 'verification_available': lambda: True}
        seen_true = {}
        daemon = self.boot()
        with mock.patch.object(plugins.PluginHost, 'load_installed', lambda self_host: spy(self_host, seen_true)):
            composed = self.compose(daemon, routes={}, plugin_options=caller_true)
        composed.close()
        daemon.close()
        self.assertEqual(sorted(caller_true), ['gil_probe', 'load', 'verification_available'])
        self.assertFalse(seen_true['socket'], 'load=True ran after the socket was already serving')
        self.assertEqual(composed.plugins.state, 'ready')

        caller_false = {'load': False, 'gil_probe': FakeGilProbe(), 'verification_available': lambda: True}
        seen_false = {}
        daemon_false = self.boot()
        with mock.patch.object(plugins.PluginHost, 'load_installed',
                               lambda self_host: spy(self_host, seen_false)):
            composed_false = self.compose(daemon_false, routes={}, plugin_options=caller_false)
        self.addCleanup(composed_false.close)
        self.assertIn('load', caller_false)
        self.assertTrue(seen_false['socket'], 'load=False ran before the socket was serving')
        self.assertEqual(composed_false.plugins.state, 'ready')


if __name__ == '__main__':
    unittest.main()
