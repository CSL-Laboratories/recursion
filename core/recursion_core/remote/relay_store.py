"""What the relay keeps on the daemon's machine: the frame window of each key, and the record of refusals.

Both live in one folder, ``relay`` under the daemon's state folder (``control.client.state_directory``),
owner-only (a real directory of this uid, mode 0700, made on first use and refused when it is anything
else). Every session of the relay is its own process (sshd starts one per SSH session), so what must hold
across sessions is kept here, each file under an exclusive ``flock`` while it is read and written.

* ``KeyWindow``: the times at which the frames of one key were forwarded inside the window, one file per
  key (``window-<key>.json``). Every session of that key shares it, so a cap of N frames in S seconds
  holds for the key, however many sessions it opens, one after another or at the same time. Times come
  from the relay's monotonic clock, read while the file's lock is held; a time later than now (a clock
  that started again, as after a restart of the machine) is dropped.
* ``RefusalRecord``: one JSON line per refusal the relay's rights and caps made (``refusals.jsonl``),
  newest last, at most ``max_entries`` lines kept (older lines are dropped first; ``None`` keeps every
  line, ``0`` keeps none). The person reads it with any text tool.

A request of the ending kind (``rights.ends_something``) reads and writes no window, and is sent whatever
these files are; only its refusal for another reason (no right to it, too large) is written to the record,
without waiting for a record another program holds.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import stat
from collections.abc import Callable

RELAY_STATE_DIR = "relay"  # <state folder>/relay
WINDOW_FILE = "window-{key}.json"
RECORD_FILE = "refusals.jsonl"
FOLDER_MODE, FILE_MODE = 0o700, 0o600
_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


class RelayStateRefused(ValueError):
    """The relay's folder on the daemon's machine is not a real owner-only directory of this uid."""


class RelayFileRefused(OSError):
    """A file or the folder of the relay's own that is no longer what the relay made, met after the start
    (a link, a FIFO, a folder or another uid's file at a file's name, or the folder swapped for something
    else). An ``OSError``, so that every caller that refuses a frame on an unreadable window or skips a
    record that cannot be written does the same here."""


def relay_folder(state_dir: str, *, uid: int | None = None) -> str:
    """``<state_dir>/relay``, made owner-only when it is missing and checked every time."""
    if not os.path.isabs(state_dir):
        raise RelayStateRefused(f"the state folder {state_dir!r} is not an absolute path")
    os.makedirs(state_dir, mode=FOLDER_MODE, exist_ok=True)
    folder = os.path.join(state_dir, RELAY_STATE_DIR)
    try:
        os.mkdir(folder, FOLDER_MODE)
    except FileExistsError:
        pass
    _check_folder(folder, RelayStateRefused, uid)
    return folder


def _check_folder(folder: str, refusal: type[Exception], uid: int | None) -> None:
    info = os.lstat(folder)  # lstat: a symlink is not a directory here, wherever it points
    owner = os.geteuid() if uid is None else uid
    if not stat.S_ISDIR(info.st_mode):
        raise refusal(f"{folder} is not a real directory")
    if info.st_uid != owner:
        raise refusal(f"{folder} is owned by uid {info.st_uid}, not {owner}")
    if stat.S_IMODE(info.st_mode) != FOLDER_MODE:
        raise refusal(f"{folder} has mode {stat.S_IMODE(info.st_mode):04o}, want 0700")


class _Locked:
    """One file of the folder, opened without following a symlink and held under an exclusive flock.

    The folder is looked at again first (a real directory of ``uid``, mode 0700), and the file opened must
    be a regular file of ``uid``: anything else is ``RelayFileRefused`` before it is read or locked, so a
    folder swapped while the relay runs, or a FIFO put at a file's name, never holds a frame. ``wait``
    false asks for the lock without waiting: a file another program holds is ``BlockingIOError`` then."""

    def __init__(self, path: str, *, uid: int | None = None, wait: bool = True) -> None:
        self.path, self.uid, self.wait = path, uid, wait

    def __enter__(self) -> int:
        _check_folder(os.path.dirname(self.path), RelayFileRefused, self.uid)
        self.fd = os.open(self.path, _OPEN_FLAGS | os.O_NONBLOCK, FILE_MODE)
        try:
            info = os.fstat(self.fd)
            owner = os.geteuid() if self.uid is None else self.uid
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
                raise RelayFileRefused(f"{self.path} is not a regular file of uid {owner}")
            fcntl.flock(self.fd, fcntl.LOCK_EX if self.wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise
        return self.fd

    def __exit__(self, *exc) -> None:
        os.close(self.fd)  # closing the descriptor lets the lock go


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _replace(fd: int, data: bytes) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    _write_at_end(fd, data)


class KeyWindow:
    """The sliding window of one key, shared by every session of that key."""

    def __init__(self, folder: str, key: str, *, uid: int | None = None) -> None:
        self.path = os.path.join(folder, WINDOW_FILE.format(key=key))
        self.uid = uid

    def take(self, clock: Callable[[], float], max_frames: int, window_s: float) -> tuple[int, float | None] | None:
        """Let one frame through and return ``None``, or return how many were already in the window and the
        seconds until the oldest of them leaves it (``None`` when none is in it: a cap of 0 never frees).

        ``clock`` is read once the lock is held, and the seconds come from the same reading. A session that
        read it before waiting for the lock would find the time a session that went first wrote meanwhile
        later than its own now, drop it as a clock that started again, and let its frame through: the cap
        would not hold for sessions at the same time. ``OSError`` when the file cannot be read or written;
        the caller refuses the frame then."""
        with _Locked(self.path, uid=self.uid) as fd:
            now = clock()
            try:
                stamps = json.loads(_read_all(fd) or b"[]")
            except ValueError:
                stamps = []  # a file cut short by a crash between truncate and write: an empty window
            if not isinstance(stamps, list):
                stamps = []
            kept = [t for t in stamps if type(t) in (int, float) and math.isfinite(t) and now - window_s < t <= now]
            if len(kept) >= max_frames:
                if len(kept) != len(stamps):
                    _replace(fd, json.dumps(kept).encode("ascii"))
                return len(kept), (min(kept) + window_s - now if kept else None)
            kept.append(now)
            _replace(fd, json.dumps(kept).encode("ascii"))
            return None


class RefusalRecord:
    """The record of refusals on the daemon's machine, at most ``max_entries`` lines (``None``: no limit)."""

    def __init__(self, folder: str, max_entries: int | None, *, uid: int | None = None) -> None:
        self.path = os.path.join(folder, RECORD_FILE)
        self.max_entries = max_entries
        self.uid = uid

    def add(self, event: dict, *, wait: bool = True) -> None:
        """Add one line. ``wait`` false: a record another program holds is ``BlockingIOError``, not waited for."""
        if self.max_entries == 0:
            return
        line = (json.dumps(event, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        with _Locked(self.path, uid=self.uid, wait=wait) as fd:
            if self.max_entries is None:
                os.lseek(fd, 0, os.SEEK_END)
                _write_at_end(fd, line)
                return
            lines = _read_all(fd).splitlines(keepends=True)
            lines.append(line)
            if len(lines) > self.max_entries:
                _replace(fd, b"".join(lines[-self.max_entries:]))
            else:
                os.lseek(fd, 0, os.SEEK_END)
                _write_at_end(fd, line)


def _write_at_end(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def read_refusals(folder: str) -> list[dict]:
    """The record as a list of refusals, oldest first (for status and for tests)."""
    try:
        with open(os.path.join(folder, RECORD_FILE), "rb") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []
