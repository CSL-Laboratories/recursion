"""Test-only process-group stand-in for the systemd user manager (CSL-738 B8/B14 cells).

Not a product spawn path. It lets the crash cells run real processes and a real
kill -9 of a real daemon process on a host where systemd is not PID 1 and no user
manager runs. Each run is its own session and process group, so it outlives the
daemon that launched it. A record file per unit (pid plus kernel start time) is the
"unit"; a reused pid with a different start time is never mistaken for the run.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

UNIT_SUFFIX = '.service'
# Bound on waiting for a SIGKILLed run to be gone after escalation.
KILL_WAIT = 5.0


def _proc_stat(pid):
    """Return (state, starttime) from /proc/<pid>/stat, or None when the pid is gone."""
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    fields = text[text.rindex(')') + 2:].split()
    return fields[0], fields[19]


class ProcessGroupManager:
    def __init__(self, directory: Path, stop_hook=None, observe_hook=None, grace=5.0):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stop_hook, self.observe_hook, self.grace = stop_hook, observe_hook, grace

    def _record(self, unit):
        try:
            return json.loads((self.directory / unit).read_text())
        except (OSError, ValueError):
            return None

    def start(self, unit, argv):
        if not argv:
            raise ValueError('Empty launch')
        process = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   close_fds=True)
        stat = None
        for _ in range(200):
            stat = _proc_stat(process.pid)
            if stat:
                break
            time.sleep(0.005)
        if stat is None:
            raise OSError('launched process vanished before it was recorded')
        tmp = self.directory / (unit + '.tmp')
        tmp.write_text(json.dumps({'pid': process.pid, 'start': stat[1]}))
        os.replace(tmp, self.directory / unit)

    def _alive(self, record):
        stat = _proc_stat(record['pid'])
        return bool(stat) and stat[1] == record['start'] and stat[0] not in ('Z', 'X')

    def stop(self, unit):
        if self.stop_hook:
            self.stop_hook('before_signal', unit)
        record = self._record(unit)
        if record is None:
            raise OSError(f'no record for {unit}')
        if self._alive(record):
            try:
                os.killpg(record['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
            if self.stop_hook:
                self.stop_hook('signal_sent', unit)
            deadline = time.monotonic() + self.grace
            while self._alive(record) and time.monotonic() < deadline:
                time.sleep(0.01)
            if self._alive(record):
                try:
                    os.killpg(record['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # SIGKILL delivery is asynchronous: return only once the run is
                # positively gone (dead or zombie), so a caller that checks right after
                # stop() never sees a run that is merely still dying (Devin r4506539119).
                deadline = time.monotonic() + KILL_WAIT
                while self._alive(record) and time.monotonic() < deadline:
                    time.sleep(0.005)
        if self.stop_hook:
            self.stop_hook('after_signal', unit)

    def observe(self, unit):
        if self.observe_hook:
            self.observe_hook(unit)
        record = self._record(unit)
        if record is None:
            return 'unknown'
        return 'running' if self._alive(record) else 'empty'

    def processes_gone(self, unit):
        """Attest from the recorded pid, not from a scripted observe() result."""
        record = self._record(unit)
        if record is None:
            return False
        return not self._alive(record)

    def owned_units(self):
        return {p.name for p in self.directory.iterdir() if p.name.endswith(UNIT_SUFFIX)}

    def pid(self, unit):
        record = self._record(unit)
        return record and record['pid']

    def alive(self, unit):
        record = self._record(unit)
        return bool(record) and self._alive(record)
