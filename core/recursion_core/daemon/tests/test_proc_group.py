"""ProcessGroupManager must count the whole process group, not only its leader.

The leader of a test run can exit while a grandchild it spawned is still alive in
that process group. Attesting the tree gone from the leader's death, or skipping
the signal because the leader is already dead, leaves the descendant running.
"""
import ctypes
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from recursion_core.daemon.tests.procmanager import ProcessGroupManager

LINUX = sys.platform.startswith('linux') and Path('/proc/self/stat').exists()


def _state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except OSError:
        return None


def _reap(pid):
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


@unittest.skipUnless(LINUX, 'process-group attestation needs Linux /proc')
class ProcessGroupTreeTests(unittest.TestCase):
    def test_manager_attests_entire_group_after_leader_exits(self):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER: reap the orphaned grandchild
            self.skipTest('this host cannot adopt orphaned descendants')
        self.addCleanup(lambda: libc.prctl(36, 0, 0, 0, 0))
        pids = []
        self.addCleanup(lambda: [_reap(pid) for pid in pids])
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            manager = ProcessGroupManager(base / 'units', grace=0.02)
            pidfile = base / 'grandchild.pid'
            code = ('import subprocess, pathlib, sys; '
                    'p=subprocess.Popen(["/bin/sleep", "4"]); '
                    'pathlib.Path(sys.argv[1]).write_text(str(p.pid))')
            unit = 'recursion-run-tree.service'
            manager.start(unit, [sys.executable, '-I', '-c', code, str(pidfile)])
            leader = manager.pid(unit)
            pids.append(leader)
            text = ''
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if pidfile.exists():
                    text = pidfile.read_text().strip()
                    if text:
                        break
                time.sleep(0.005)
            self.assertTrue(text, 'the leader never wrote the grandchild pid')
            grandchild = int(text)
            pids.append(grandchild)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and _state(leader) not in (None, 'Z', 'X'):
                time.sleep(0.005)
            self.assertIn(_state(leader), (None, 'Z', 'X'), 'the leader did not exit')
            self.assertNotIn(_state(grandchild), (None, 'Z', 'X'))
            # The leader is already dead. A live member of its group is not a gone tree.
            self.assertFalse(manager.processes_gone(unit))
            manager.stop(unit)
            attested = manager.processes_gone(unit)
            alive = _state(grandchild) not in (None, 'Z', 'X')
            self.assertFalse(attested and alive)
            self.assertFalse(alive, 'the descendant outlived stop')
