"""ProcessGroupManager must collect the direct child it started.

stop signals that child and waits until it is dead or a zombie. Leaving the
zombie unreaped keeps its pid in /proc. The manager started the child, so it
has to collect that pid. A pid it did not start is not its child. The second
test calls the collector only, so a recorded group is not signalled.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from recursion_core.daemon.tests.procmanager import ProcessGroupManager

LINUX = sys.platform.startswith('linux') and Path('/proc/self/stat').exists()


def _state(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except OSError:
        return None


def _collect(pid):
    """Collect this test's own child if stop left it. Do not wait any other pid."""
    if not pid:
        return
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


@unittest.skipUnless(LINUX, 'direct-child collection needs Linux /proc')
class DirectChildReapTests(unittest.TestCase):
    def test_manager_reaps_its_direct_child(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessGroupManager(Path(temp) / 'units', grace=0.02)
            unit = 'recursion-run-zombie.service'
            manager.start(unit, ['/bin/sleep', '4'])
            pid = manager.pid(unit)
            self.addCleanup(_collect, pid)
            manager.stop(unit)
            state = _state(pid)
            self.assertIsNone(state, f'direct child still in /proc after stop (state {state})')

    def test_a_pid_this_manager_did_not_start_is_not_collected(self):
        child = subprocess.Popen(
            ['/bin/sleep', '30'], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(_collect, child.pid)
        with tempfile.TemporaryDirectory() as temp:
            manager = ProcessGroupManager(Path(temp) / 'units', grace=0.02)
            stat = Path(f'/proc/{child.pid}/stat').read_text()
            start = stat[stat.rindex(')') + 2:].split()[19]
            unit = 'recursion-run-adopted.service'
            (manager.directory / unit).write_text(json.dumps({'pid': child.pid, 'start': start}))
            manager._reap_direct_child(manager._record(unit))
            self.assertIsNone(child.poll(), 'a re-adopted pid was collected')
        child.kill()
        child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
