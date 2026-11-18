"""Leader identity for a Darwin run: pid, process group, session and start time.

On this Linux host the start time is field 22 of ``/proc/<pid>/stat`` (clock
ticks since boot). A Mac has no ``/proc``. ``read`` then returns None and the
launcher refuses, rather than storing a start time it did not read. The fake
holds the row the launcher recorded, including after the pid has exited, and
can present a different start time so a reused pid is refused.
"""

from __future__ import annotations

import os
from pathlib import Path


def _proc_start_time(pid: int) -> int | None:
    try:
        text = Path('/proc/%d/stat' % pid).read_text()
    except OSError:
        return None
    try:
        # Fields after the comm: index 19 is starttime (man proc, field 22).
        rest = text[text.rindex(')') + 2:].split()
        return int(rest[19])
    except (ValueError, IndexError):
        return None


class ProcTable:
    """The host's process table. ``/proc`` on Linux; nothing on a Mac."""

    def __init__(self) -> None:
        self.launches: list[dict] = []

    def read(self, pid: int) -> dict | None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        start = _proc_start_time(pid)
        if start is None:
            return None
        try:
            pgid = os.getpgid(pid)
            sid = os.getsid(pid)
        except OSError:
            return None
        return {'start_time': start, 'pgid': pgid, 'sid': sid}

    def start_time(self, pid: int) -> int | None:
        got = self.read(pid)
        if not got:
            return None
        return got.get('start_time')

    def group_members(self, pgid: int, name: str, cookie: str) -> list[int]:
        """Live pids in process group ``pgid`` whose environment holds ``name=cookie``.

        A Mac has no ``/proc``: nothing is found there.
        """

        if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
            return []
        needle = ('%s=%s' % (name, cookie)).encode()
        try:
            entries = os.listdir('/proc')
        except OSError:
            return []
        found = []
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                if os.getpgid(pid) != pgid:
                    continue
                environ = Path('/proc/%d/environ' % pid).read_bytes()
            except OSError:
                continue
            if needle in environ.split(b'\0'):
                found.append(pid)
        return found

    def record_launch(self, **info: object) -> None:
        self.launches.append(dict(info))


class FakeProcessTable:
    """A process table the test can spoil.

    While ``/proc`` still has the pid, reads come from there. After the leader
    exits, the row recorded at launch is what ``start_time`` returns, until
    ``spoof_start_time`` presents a different one.
    """

    def __init__(self, inner: ProcTable | None = None) -> None:
        self.inner = inner if inner is not None else ProcTable()
        self.launches: list[dict] = []
        self._held: dict[int, dict] = {}
        self._spoof: dict[int, int] = {}

    def read(self, pid: int) -> dict | None:
        if pid in self._spoof:
            held = self._held.get(pid, {})
            return {'start_time': self._spoof[pid], 'pgid': held.get('pgid'), 'sid': held.get('sid')}
        live = self.inner.read(pid)
        if live is not None:
            self._held[pid] = live
            return live
        return self._held.get(pid)

    def start_time(self, pid: int) -> int | None:
        got = self.read(pid)
        if not got:
            return None
        return got.get('start_time')

    def group_members(self, pgid: int, name: str, cookie: str) -> list[int]:
        return self.inner.group_members(pgid, name, cookie)

    def record_launch(self, **info: object) -> None:
        self.launches.append(dict(info))
        pid = info.get('pid')
        if isinstance(pid, int) and not isinstance(pid, bool):
            self._held[pid] = {
                'start_time': info.get('start_time'),
                'pgid': info.get('pgid'),
                'sid': info.get('sid'),
            }

    def spoof_start_time(self, pid: int, start_time: int) -> None:
        """Present ``pid`` as a different process that reused the number."""

        self._spoof[pid] = start_time
