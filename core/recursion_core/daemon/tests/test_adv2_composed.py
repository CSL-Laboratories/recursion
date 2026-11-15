"""ADV2 red-team on the composed daemon: a breaker store that breaks between two daemon starts.

S34 decision 6: an unreadable breaker store "refuses every start (``breaker_store_unavailable``)
and keeps reads and Stop". test_composition.py checks the reads; this checks the Stop of a run the
earlier daemon launched, through the real socket, with the stand-in engine and the same
``ProcessUnitManager`` stand-in for systemd that test_composition.py uses.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from recursion_core.control.client import SocketAPI
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import breaker as daemon_breaker
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon.tests.test_composition import BOOT, STANDIN, ProcessUnitManager, Units, until

LINUX = sys.platform.startswith('linux')
LINUX_ONLY = 'Linux-only: the composed daemon needs pidfd, FIFO stdio and AF_UNIX peer credentials'


@unittest.skipUnless(LINUX, LINUX_ONLY)
class Adv2BrokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv2-compose-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state, self.runtime = self.tmp / 'state', self.tmp / 'rt'
        self.runtime.mkdir(mode=0o700)
        (self.tmp / 'bin').mkdir(mode=0o700)
        self.engine = self.tmp / 'bin' / 'claude'
        shutil.copyfile(STANDIN, self.engine)
        os.chmod(self.engine, 0o700)
        try:
            probe = subprocess.run([str(self.engine)], stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                                   env={'PATH': '/usr/bin:/bin'}, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            probe = b''
        if b'stand-in engine got no user message' not in probe:
            self.skipTest('temp directory cannot execute the stand-in engine (noexec mount or no /usr/bin/python3)')
        self.units = Units()
        self.addCleanup(self.kill_units)
        self.environ = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp), 'LANG': 'C.UTF-8',
                        'XDG_RUNTIME_DIR': str(self.runtime), 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
        self.routes = {'claude_code': {
            'route': {'engine': 'claude_code', 'model': 'stand-in-model', 'api_key_helper': 'absent',
                      'connection': {'kind': 'subscription', 'id': 'stand-in-login'}},
            'deadline_s': 120.0, 'binary': str(self.engine)}}
        self.open = []
        self.addCleanup(self.shutdown)

    def kill_units(self):
        for proc in self.units.procs.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()

    def boot(self):
        daemon = Daemon(Registry(self.state), ProcessUnitManager(self.units), boot_id=lambda: BOOT)
        daemon.sweep()
        composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes=self.routes)
        composed.control.reconcile()
        self.open.append((daemon, composed))
        return daemon, composed

    def shutdown(self):
        while self.open:
            daemon, composed = self.open.pop()
            composed.close()
            daemon.close()

    def test_stop_of_an_earlier_run_works_when_the_store_became_unreadable(self):
        _, composed = self.boot()
        api = SocketAPI(composed.server.path, timeout_s=30)
        answer = api.call('run.start', {'tenant': 't1', 'engine': 'claude_code', 'prompt': 'helper=0 slow=1'})
        self.assertEqual(answer['code'], 'started', answer)
        run_id = answer['run']['run_id']
        self.shutdown()  # the run outlives the daemon

        store = daemon_breaker.store_path(self.state)
        for side in (Path(f'{store}-wal'), Path(f'{store}-shm')):
            if side.exists():
                side.unlink()
        store.write_bytes(b'not a breaker store any more')
        with self.assertLogs('recursion_core.daemon.control_adapter', 'ERROR'):
            daemon, composed = self.boot()
        api = SocketAPI(composed.server.path, timeout_s=30)
        self.assertEqual(api.call('route.list')['breaker_store'], 'unavailable: breaker_store_invalid')
        refused = api.call('run.start', {'tenant': 't2', 'engine': 'claude_code', 'prompt': 'helper=0 fast=1'})
        self.assertEqual(refused['start_error']['code'], 'breaker_store_unavailable')
        self.assertEqual(api.call('run.status', {'run_id': run_id})['run']['state'], 'running')
        stopped = until(lambda: (lambda a: a if a.get('code') == 'stopped' else None)(
            api.call('run.stop', {'run_id': run_id})), 30)
        self.assertEqual(stopped['code'], 'stopped', stopped)
        self.assertEqual(len(self.units.started), 1, 'the refused start launched nothing')
        self.assertEqual(store.read_bytes(), b'not a breaker store any more')


if __name__ == '__main__':
    unittest.main()
