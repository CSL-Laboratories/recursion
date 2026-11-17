"""Desired-behavior regression tests; baseline failures are sweep witnesses.

Run with PYTHONPATH=<pinned checkout>/core. No real user manager or vendor runs.
All real sleepers are four seconds or less, with explicit kill/wait cleanup.
"""
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from recursion_core.cgroup_leaf import DelegatedRoot
from recursion_core.daemon import Daemon, Registry, Run, launcher
from recursion_core.daemon import control_adapter
from recursion_core.daemon import engine_user
from recursion_core.daemon.tests.procmanager import ProcessGroupManager
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import InMemoryRunRegistry


def observe(name, **values):
    print('OBS ' + name + ' ' + json.dumps(values, sort_keys=True), flush=True)


def await_value(fn, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError('bounded fixture did not become ready')


def proc_state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except FileNotFoundError:
        return None


def reap(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process ownership witnesses')
class ScratchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='s6-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

class ExitStatusTests(ScratchTest):
    def test_F12_signal_killed_probe_cannot_attest_inactive_manager(self):
        accepted = []
        for returncode in (-signal.SIGKILL, 128 + signal.SIGKILL):
            call = mock.Mock(side_effect=[
                subprocess.CompletedProcess([], 0, 'no\n', ''),
                subprocess.CompletedProcess([], returncode, 'inactive\n', ''),
            ])
            try:
                engine_user.check_engine_no_user_manager('engine', 12345, operator_uid=999,
                                                          call=call, runtime_root=self.base)
            except engine_user.EngineManagerPresent:
                pass
            else:
                accepted.append(returncode)
        observe('F12', accepted_abnormal_returncodes=accepted)
        self.assertEqual(accepted, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
