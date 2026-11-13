"""Shared fixtures for the ADV1 adversarial tests (no tests here).

The composed daemon is built exactly as ``test_composition.ComposedDaemonTest`` builds it: the
production control adapter, launcher, registry, socket and CLI, with ``ProcessUnitManager`` in
place of ``systemd-run``. Only helpers are imported from ``test_composition``, never its test
classes, so its tests are not collected twice.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from recursion_core.control.client import SocketAPI
from recursion_core.daemon import Daemon, Registry
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon.tests.test_composition import BOOT, STANDIN, ProcessUnitManager, Units, until

__all__ = ['BOOT', 'STANDIN', 'ComposedBase', 'ProcessUnitManager', 'Units', 'until']


class ComposedBase(unittest.TestCase):
    """setUp / boot / shutdown of a composed daemon. Subclasses add the tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv1-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.state = self.tmp / 'state'
        self.runtime = self.tmp / 'rt'
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

    def manager(self):
        return ProcessUnitManager(self.units)

    def boot(self, manager=None, **options):
        daemon = Daemon(Registry(self.state), manager or self.manager(), boot_id=lambda: BOOT)
        daemon.sweep()
        composed = adapter.compose_control(daemon, self.state, environ=self.environ, routes=self.routes, **options)
        composed.control.reconcile()
        self.open.append((daemon, composed))
        return daemon, composed

    def shutdown(self):
        while self.open:
            daemon, composed = self.open.pop()
            composed.close()
            daemon.close()

    def api(self, composed, timeout_s=30):
        return SocketAPI(composed.server.path, timeout_s=timeout_s)

    def start(self, api, prompt='helper=0', key=None):
        params = {'tenant': 't1', 'engine': 'claude_code', 'prompt': prompt}
        if key:
            params['client_key'] = key
        answer = api.call('run.start', params)
        self.assertEqual(answer['code'], 'started', answer)
        return answer['run']['run_id']

    def lines(self, run_id):
        path = self.state / 'runs' / run_id / 'stdout.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def wait_for(predicate, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())
