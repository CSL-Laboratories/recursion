"""A kqueue watch for one run leader's exit.

``NOTE_TRACK``, ``NOTE_TRACKERR`` and ``NOTE_CHILD`` are not used. On XNU those
notes return ``ENOTSUP``, so a watch that asked for them would not attach.
``exit_watch`` is the only pair this launcher registers: ``EVFILT_PROC`` and
``NOTE_EXIT``.
"""

from __future__ import annotations

import errno
import os
import select

# Values from XNU's sys/event.h. Linux has no kqueue; the fake records these.
EVFILT_PROC = -5
NOTE_EXIT = 0x80000000
NOTE_TRACK = 0x00000001
NOTE_TRACKERR = 0x00000002
NOTE_CHILD = 0x00000004

_REFUSED_NOTES = NOTE_TRACK | NOTE_TRACKERR | NOTE_CHILD


def exit_watch() -> tuple[int, int]:
    """``(filter, fflags)`` for a leader exit. Never includes ``NOTE_TRACK``."""

    return EVFILT_PROC, NOTE_EXIT


class FakeKqueue:
    """Records the watch and returns a pipe the test can make readable.

    ``report_exit`` writes one byte, which is how this Linux host stands in for
    the kernel delivering ``NOTE_EXIT``. It is not a kqueue.
    """

    def __init__(self) -> None:
        self.watches: list[dict] = []
        self._write: dict[int, int] = {}

    def watch_exit(self, pid: int) -> int:
        filt, flags = exit_watch()
        if flags & _REFUSED_NOTES:
            raise OSError(errno.ENOTSUP, 'process note is not attached on XNU')
        read_fd, write_fd = os.pipe()
        self.watches.append({'pid': pid, 'filter': filt, 'fflags': flags, 'fd': read_fd})
        self._write[pid] = write_fd
        return read_fd

    def report_exit(self, pid: int) -> None:
        fd = self._write.get(pid)
        if fd is None:
            raise KeyError(pid)
        os.write(fd, b'\n')

    def close(self) -> None:
        for fd in list(self._write.values()):
            try:
                os.close(fd)
            except OSError:
                pass
        self._write.clear()
        for watch in self.watches:
            try:
                os.close(watch['fd'])
            except OSError:
                pass
        self.watches.clear()


class XnuKqueue:
    """A real kqueue. Only constructed where ``select.kqueue`` exists (a Mac)."""

    def __init__(self) -> None:
        self._open: list[object] = []

    def watch_exit(self, pid: int) -> int:
        filt, flags = exit_watch()
        if flags & _REFUSED_NOTES or filt != EVFILT_PROC:
            raise OSError(errno.ENOTSUP, 'process note is not attached on XNU')
        if not hasattr(select, 'kqueue') or not hasattr(select, 'KQ_NOTE_EXIT'):
            raise OSError(errno.ENOTSUP, 'kqueue')
        kq = select.kqueue()
        # KQ_NOTE_EXIT only. Do not OR in KQ_NOTE_TRACK: XNU returns ENOTSUP.
        event = select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
            fflags=select.KQ_NOTE_EXIT,
        )
        kq.control([event], 0)
        self._open.append(kq)  # the fd dies if this object is collected
        return kq.fileno()
