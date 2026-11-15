"""The owner-only folder and its files (POSIX only; the store refuses elsewhere before it gets here).

The folder is 0700 and owned by the user who runs the store; every file in it is a regular file, 0600,
owned by that user, with one link. Each is checked on EVERY open, through the descriptor that was opened
(``fstat``), so a check and the use that follows it see the same object:

- the folder is opened with ``O_DIRECTORY | O_NOFOLLOW``: a symlink in its place (dangling, or pointing
  at another folder of the user's) or a file is refused, and so is a folder of another mode or owner;
  nothing is chmodded to make it fit;
- every file is opened relative to that descriptor with ``O_NOFOLLOW | O_NONBLOCK``: a symlink is
  refused, and a FIFO planted in a file's place cannot block the open; it is then refused as not regular;
- a file is written to a fresh temp file (``O_CREAT | O_EXCL``, 0600 whatever the umask), synced, renamed
  over the old one and the folder synced, so a reader without the lock sees the old file or the new one,
  never a part of either;
- writers take ``flock`` on the folder's ``.lock`` file, so two saves at once do not lose one another.

The folders ABOVE the store's own are not checked. Someone who can write one of them can rename the
store's folder away or put something in its place, but cannot make a folder or a file owned by this user,
and anything else is refused here; they can stop the store working, which they could do anyway, and cannot
read a key or make the store write one where they can read it. A check of every ancestor would also refuse
the common home where ``~/.config`` is group-writable under a per-user group.

Folders that do not exist are made only on a write (``open_folder(create=True)``): each missing one is made
0700. A read never creates anything.
"""
from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from pathlib import Path
from typing import Self

from recursion_core.keys.errors import KeyStoreError

FOLDER_MODE = 0o700
FILE_MODE = 0o600
LOCK_NAME = '.lock'

# getattr: these names do not exist on Windows, where nothing below runs (the store refuses first).
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)


def _why(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__


class Folder:
    """An open, checked descriptor of the store's folder. Use as a context manager; it closes the descriptor."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path, self.fd = path, fd

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f'Folder({str(self.path)!r})'

    # -- files ----------------------------------------------------------------------------------
    def read(self, name: str, limit: int) -> bytes | None:
        """The bytes of an owner-only file in the folder, or None when there is no such file."""
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise KeyStoreError('file_invalid', f'{self.path / name}: cannot be opened ({_why(exc)})') from None
        try:
            check_file(fd, self.path / name)
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise KeyStoreError('file_invalid', f'{self.path / name}: larger than {limit} bytes')
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            os.close(fd)

    def write(self, name: str, data: bytes) -> None:
        """Replace ``name`` with ``data``: a temp file, fsync, rename, fsync of the folder."""
        temp = f'.{name}.{secrets.token_hex(8)}.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, FILE_MODE, dir_fd=self.fd)
        try:
            try:
                os.fchmod(fd, FILE_MODE)       # 0600 whatever the umask made of the creation mode
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=self.fd)
            raise
        os.fsync(self.fd)

    @contextlib.contextmanager
    def locked(self, *, shared: bool = False):
        """Hold the folder's lock (``flock`` on ``.lock``) for the block: exclusive for a writer, shared for a reader.

        A writer creates the lock file if it is missing. A reader creates nothing: with no lock file there has never
        been a save in this folder, and the block runs with no lock held (``yield False``); the reader then finds no
        index, or refuses one that exists without a lock beside it.
        """
        import fcntl

        flags = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC | (0 if shared else os.O_CREAT)
        try:
            # Read-only is enough for flock, and opens a lock a umask left at 0400.
            fd = os.open(LOCK_NAME, flags, FILE_MODE, dir_fd=self.fd)
        except FileNotFoundError:
            fd = None
        except OSError as exc:
            raise KeyStoreError('file_invalid', f'{self.path / LOCK_NAME}: cannot be opened ({_why(exc)})') from None
        if fd is None:
            yield False
            return
        try:
            st = os.fstat(fd)
            # The lock holds nothing; a umask may have made it 0400 when it was created, so a lock that is
            # only narrower than 0600 is put back to 0600. Anything wider, or not ours, is refused.
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1
                    or stat.S_IMODE(st.st_mode) & 0o077):
                raise KeyStoreError('file_invalid', f'{self.path / LOCK_NAME}: not an owner-only file')
            if stat.S_IMODE(st.st_mode) != FILE_MODE:
                os.fchmod(fd, FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield True
        finally:
            os.close(fd)        # releases the lock


def check_file(fd: int, path: Path) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise KeyStoreError('file_invalid', f'{path}: not a regular file')
    if st.st_uid != os.geteuid():
        raise KeyStoreError('file_invalid', f'{path}: not owned by this user')
    if stat.S_IMODE(st.st_mode) != FILE_MODE:
        raise KeyStoreError('file_invalid', f'{path}: mode {stat.S_IMODE(st.st_mode):04o}, not 0600')
    if st.st_nlink != 1:
        raise KeyStoreError('file_invalid', f'{path}: has {st.st_nlink} links, not 1')


def open_folder(path: Path, *, create: bool) -> Folder | None:
    """The checked folder, or None when it does not exist and ``create`` is False."""
    path = Path(path)
    if not path.is_absolute():
        raise KeyStoreError('folder_invalid', f'{path}: not an absolute path')
    if create:
        _make(path)
    try:
        fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
    except FileNotFoundError:
        if create:
            raise KeyStoreError('folder_invalid', f'{path}: vanished while it was made') from None
        return None
    except OSError as exc:
        raise KeyStoreError('folder_invalid', f'{path}: not a folder this store can use ({_why(exc)})') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise KeyStoreError('folder_invalid', f'{path}: not a folder')
        if st.st_uid != os.geteuid():
            raise KeyStoreError('folder_invalid', f'{path}: not owned by this user')
        if stat.S_IMODE(st.st_mode) != FOLDER_MODE:
            raise KeyStoreError('folder_invalid', f'{path}: mode {stat.S_IMODE(st.st_mode):04o}, not 0700')
    except BaseException:
        os.close(fd)
        raise
    return Folder(path, fd)


def _make(path: Path) -> None:
    """Make each missing folder on the way to ``path``, 0700. An existing one is left as it is."""
    missing = []
    probe = path
    while not os.path.lexists(probe):      # lexists: a dangling symlink exists and is refused when opened
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for folder in reversed(missing):
        try:
            os.mkdir(folder, FOLDER_MODE)
        except FileExistsError:
            continue                        # made by someone else meanwhile: checked when it is opened
        except OSError as exc:
            raise KeyStoreError('folder_invalid', f'{folder}: cannot be made ({_why(exc)})') from None
        # mkdir's mode passes through the umask: never wider than 0700, perhaps narrower. Make it exactly
        # 0700 through a descriptor of the folder just made (no path is followed for it).
        try:
            fd = os.open(folder, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
        except OSError as exc:
            raise KeyStoreError('folder_invalid', f'{folder}: cannot be opened ({_why(exc)})') from None
        try:
            if os.fstat(fd).st_uid == os.geteuid():
                os.fchmod(fd, FOLDER_MODE)
        finally:
            os.close(fd)
