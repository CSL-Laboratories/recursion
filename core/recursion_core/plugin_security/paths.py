"""Filesystem paths for plugin files and installer stores.

A store path is turned into one absolute filename before it is measured or opened.
SQLite's default ``SQLITE_MAX_PATHNAME`` is 512 bytes, and opening a database also
names ``<path>-journal``. Each component must also fit in ``NAME_MAX``. ``:memory:``
and ``file:`` URIs are refused: this process's SQLite build treats those strings as
databases, not as the filename ``os.open`` just created.

Plugin files are read from a directory file descriptor with ``O_NOFOLLOW``. The bytes
come from that descriptor. A symlink is a refusal, not a path that is opened later.
"""

from __future__ import annotations

import errno
import os
import stat

from .errors import Refused

__all__ = ["abandon_store", "check_store_path", "open_new_store", "read_within_root"]

_JOURNAL = b"-journal"
_SQLITE_MAX_PATHNAME = 512


def check_store_path(path: object) -> str:
    """Return the absolute filename SQLite would open, or refuse before it is created."""
    if type(path) is not str or not path or "\0" in path:
        raise Refused("store_path_invalid")
    if path == ":memory:" or path.startswith("file:"):
        raise Refused("store_path_invalid")
    try:
        os.fsencode(path)
        parent = os.path.dirname(path) or "."
        name = os.path.basename(path)
        if not name or name in (".", "..") or name.startswith("file:"):
            raise Refused("store_path_invalid")
        parent_real = os.path.realpath(parent)
        resolved = os.path.join(parent_real, name)
        encoded = os.fsencode(resolved)
        limit = _name_max(parent_real)
        for part in resolved.split(os.sep):
            if part and len(os.fsencode(part)) > limit:
                raise Refused("store_path_too_long")
    except Refused:
        raise
    except UnicodeEncodeError:
        raise Refused("store_path_not_encodable") from None
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise Refused("store_path_too_long") from None
        raise Refused("store_path_invalid") from None
    if len(encoded) + len(_JOURNAL) > _SQLITE_MAX_PATHNAME:
        raise Refused("store_path_too_long")
    return resolved


def open_new_store(path: str) -> str:
    """Create an empty ``0600`` file at the resolved store path. The final name is not followed."""
    resolved = check_store_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(resolved, flags, 0o600)
    except UnicodeEncodeError:
        raise Refused("store_path_not_encodable") from None
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise Refused("store_path_too_long") from None
        if exc.errno == errno.ELOOP:
            raise Refused("store_path_invalid") from None
        raise
    os.close(fd)
    return resolved


def abandon_store(path: str) -> None:
    """Remove a store file left behind by a failed create, including SQLite's sidecars."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def read_within_root(root: str, declared: str) -> bytes:
    """Read ``declared`` from inside ``root``.

    The walk uses ``open`` relative to a directory descriptor and ``O_NOFOLLOW``, then
    ``fstat`` of that same descriptor. A symlink, an absolute path, or ``..`` is refused.
    The returned bytes are what that descriptor read, so replacing the name with a
    symlink after a path check cannot change them.
    """
    if type(root) is not str or type(declared) is not str or "\0" in root or "\0" in declared:
        raise Refused("plugin_path_invalid")
    if os.path.isabs(declared):
        raise Refused("plugin_path_escapes_root")
    parts = declared.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise Refused("plugin_path_escapes_root")
    try:
        os.fsencode(root)
        for part in parts:
            os.fsencode(part)
        info = os.lstat(root)
    except UnicodeEncodeError:
        raise Refused("plugin_path_unreadable") from None
    except OSError:
        raise Refused("plugin_root_invalid") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused("plugin_root_invalid")
    root_fd = _open_dir(root)
    try:
        pinned = os.fstat(root_fd)
        if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("plugin_root_invalid")
        return _read_from(root_fd, parts)
    finally:
        os.close(root_fd)


def _name_max(directory: str) -> int:
    probe = directory
    while probe and probe != os.sep and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return os.statvfs(probe or os.sep).f_namemax
    except OSError:
        return 255


def _open_dir(path: str) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise Refused("plugin_root_invalid") from None


def _read_from(root_fd: int, parts: list[str]) -> bytes:
    dir_fd = root_fd
    file_fd = None
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            if not last:
                flags |= os.O_DIRECTORY
            try:
                fd = os.open(part, flags, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise Refused("plugin_path_escapes_root") from None
                raise Refused("plugin_path_unreadable") from None
            try:
                info = os.fstat(fd)
            except OSError:
                os.close(fd)
                raise Refused("plugin_path_unreadable") from None
            if last:
                if not stat.S_ISREG(info.st_mode):
                    os.close(fd)
                    raise Refused("plugin_path_escapes_root")
                file_fd = fd
            else:
                if not stat.S_ISDIR(info.st_mode):
                    os.close(fd)
                    raise Refused("plugin_path_escapes_root")
                if dir_fd != root_fd:
                    os.close(dir_fd)
                dir_fd = fd
        chunks: list[bytes] = []
        assert file_fd is not None
        while True:
            block = os.read(file_fd, 65536)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if dir_fd != root_fd:
            os.close(dir_fd)
