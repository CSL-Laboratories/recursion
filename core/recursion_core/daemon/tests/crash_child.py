"""Daemon process for the kill -9 cells. Run as: python crash_child.py <root> <run_id> <phase>.

phase running: start the run, write the marker, then wait to be killed (B8 cell).
phase before_signal: start, then Stop; block inside the manager after the Stop intent
  committed and before any signal reaches the run (B14 cell).
phase after_signal: start, then Stop; block after the run was signalled and before the
  daemon observed it gone, i.e. before confirmation (B14 cell).
"""
from pathlib import Path
import os
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from recursion_core.daemon import Daemon, Registry, UserLock  # noqa: E402
from recursion_core.daemon.tests.procmanager import ProcessGroupManager  # noqa: E402

RUN_ARGV = [sys.executable, '-c', 'import time\nwhile True: time.sleep(1)']


def main():
    root, run_id, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    marker = root / 'marker'

    def block(point, unit):
        if point == phase:
            marker.write_text(point)
            while True:
                time.sleep(60)

    with UserLock(root / 'runtime'):
        manager = ProcessGroupManager(root / 'units', stop_hook=block)
        daemon = Daemon(Registry(root / 'state'), manager)
        daemon.start(run_id, RUN_ARGV)
        if phase == 'running':
            marker.write_text('running')
            while True:
                time.sleep(60)
        daemon.stop(run_id)
        marker.write_text('returned')  # the hook should have blocked; parent fails the cell


if __name__ == '__main__':
    os.umask(0o077)
    main()
