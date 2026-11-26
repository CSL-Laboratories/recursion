"""Desired-behavior regression tests; baseline failures are sweep witnesses.

Run with PYTHONPATH=<pinned checkout>/core. No real user manager or vendor runs.
All real sleepers are four seconds or less, with explicit kill/wait cleanup.
"""
import ctypes
import errno
try:
    import fcntl
except ImportError:  # a Windows-shaped host has no fcntl; the witness below skips
    fcntl = None
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


@unittest.skipUnless(sys.platform.startswith('linux') and fcntl is not None,
                     'Linux process ownership witnesses need fcntl')
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

    def test_inactive_text_with_the_wrong_exit_names_the_exit(self):
        call = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, 'no\n', ''),
            subprocess.CompletedProcess([], 0, 'inactive\n', ''),
        ])
        with self.assertRaises(engine_user.EngineManagerPresent) as raised:
            engine_user.check_engine_no_user_manager(
                'engine', 12345, operator_uid=999, call=call, runtime_root=self.base)
        text = str(raised.exception)
        self.assertIn("(exit 0)", text)
        self.assertIn("not inactive (exit 3)", text)
        self.assertNotIn("is 'inactive', not inactive", text)
        # A second caller with the attesting exit still passes.
        call_ok = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, 'no\n', ''),
            subprocess.CompletedProcess([], 3, 'inactive\n', ''),
        ])
        evidence = engine_user.check_engine_no_user_manager(
            'engine', 12345, operator_uid=999, call=call_ok, runtime_root=self.base)
        self.assertEqual('inactive', evidence['user_manager'])


class FcntlImportTests(unittest.TestCase):
    def test_the_module_imports_when_fcntl_is_absent(self):
        code = (
            "import sys\n"
            "sys.modules['fcntl'] = None\n"
            "import importlib\n"
            "mod = importlib.import_module('recursion_core.daemon.tests.test_sweep_s6_proc')\n"
            "assert mod.fcntl is None\n"
            "print('IMPORTED')\n"
        )
        proc = subprocess.run(
            [sys.executable, '-B', '-c', code],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn('IMPORTED', proc.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
