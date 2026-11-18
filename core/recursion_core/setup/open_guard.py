"""One gate for reading a settings file. ``observe`` and ``import`` share it.

The containment check is #564's ``_inside``: the real path must stay strictly
inside the named root. The denied-directory check uses that same real path,
including when the denied directory itself is a symlink. A file is read only
when lstat before the open and fstat on the same descriptor both say one link
and the same inode. Otherwise no byte is returned.

The file is owned by the calling user and has no group- or world-write bit,
checked on lstat before the open and again on the held descriptor.

The read walks the checked real path from the root, one directory descriptor
per part, each opened with ``O_NOFOLLOW`` and ``O_DIRECTORY``, and opens the
file relative to its parent's descriptor. The file opened is the file checked.
Where ``O_NOFOLLOW``, ``O_DIRECTORY`` or a ``dir_fd`` open is missing, the read
is refused (``platform_unsupported``) before any open. The check is not dropped.
"""

from __future__ import annotations

import os
import stat

from recursion_core.setup.deny import denied_paths, is_denied


class GateRefused(Exception):
    """The file was not read. ``reason`` is a plain word and carries no file byte."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _walk_flags():
    """The directory and file flags of the walk, or None where this platform lacks them."""

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "geteuid"):
        return None
    if os.open not in os.supports_dir_fd:
        return None
    base = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    return base | os.O_DIRECTORY, base


def _foreign(info) -> str | None:
    """A refusal word when the file is not the caller's own or others may write it."""

    if info.st_uid != os.geteuid():
        return "not_owner"
    if stat.S_IMODE(info.st_mode) & 0o022:
        return "writable"
    return None


def read_regular_file(path: str, *, root: str, home: str, max_bytes: int, denied_ids=None) -> bytes:
    """Return the file bytes, or raise ``GateRefused`` without a useful payload.

    ``denied_ids`` is the optional inode set of denied paths. It does not replace
    the one-link rule: a hard link is refused because its link count is not one.
    """

    flags = _walk_flags()
    if flags is None:
        raise GateRefused("platform_unsupported")
    if type(path) is not str or type(root) is not str or not os.path.isabs(root):
        raise GateRefused("outside")
    if type(home) is not str or not home:
        raise GateRefused("denied")
    try:
        info = os.lstat(path)
    except OSError:
        raise GateRefused("unreadable") from None
    if stat.S_ISLNK(info.st_mode):
        raise GateRefused("not_followed")
    if not stat.S_ISREG(info.st_mode):
        raise GateRefused("unreadable")
    # Visible before the open, so an alias is not read at all.
    # A platform that cannot say how many links a file has is refused here,
    # rather than opened without that check.
    try:
        nlink = info.st_nlink
    except AttributeError:
        raise GateRefused("posix") from None
    if nlink != 1:
        raise GateRefused("aliased")
    real = _real(path)
    if real is None or not _inside_real(real, root):
        raise GateRefused("outside")
    if _real_denied(path, home, real) or (denied_ids and (info.st_dev, info.st_ino) in denied_ids):
        raise GateRefused("denied")
    refused = _foreign(info)
    if refused is not None:
        raise GateRefused(refused)
    if (info.st_mode & 0o400) == 0 or info.st_size > max_bytes:
        raise GateRefused("unreadable")
    try:
        fd = _open_walk(real, *flags)
    except OSError:
        raise GateRefused("unreadable") from None
    try:
        try:
            held = os.fstat(fd)
        except OSError:
            raise GateRefused("unreadable") from None
        try:
            held_nlink = held.st_nlink
            same = held.st_dev == info.st_dev and held.st_ino == info.st_ino
        except AttributeError:
            raise GateRefused("posix") from None
        if not stat.S_ISREG(held.st_mode) or held_nlink != 1 or not same:
            raise GateRefused("aliased")
        if denied_ids and (held.st_dev, held.st_ino) in denied_ids:
            raise GateRefused("denied")
        refused = _foreign(held)
        if refused is not None:
            raise GateRefused(refused)
        if held.st_size > max_bytes:
            raise GateRefused("unreadable")
        chunks = []
        total = 0
        while True:
            try:
                block = os.read(fd, 65536)
            except OSError:
                raise GateRefused("unreadable") from None
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise GateRefused("unreadable")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _open_walk(real: str, dir_flags: int, file_flags: int) -> int:
    """Open ``real`` from the root, one no-follow directory descriptor per part."""

    parts = [part for part in real.split(os.sep) if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise OSError("not a plain absolute path")
    current = os.open(os.sep, dir_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, dir_flags, dir_fd=current)
            os.close(current)
            current = child
        return os.open(parts[-1], file_flags, dir_fd=current)
    finally:
        os.close(current)


def _real(path: str) -> str | None:
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    if type(real) is not str or not os.path.isabs(real):
        return None
    return real


def inside_root(path: str, root: str) -> bool:
    """True when the real path of ``path`` is strictly inside the real path of ``root``.

    An ancestor symlink that leaves ``root`` is false. This is #564's ``_inside``.
    """

    real = _real(path)
    return real is not None and _inside_real(real, root)


def _inside_real(real: str, root: str) -> bool:
    try:
        real_root = os.path.realpath(root)
    except (OSError, ValueError):
        return False
    if type(real) is not str or type(real_root) is not str or not real_root:
        return False
    if real == real_root:
        return False
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    return real.startswith(prefix)


def _real_denied(path: str, home: str, real: str | None) -> bool:
    """Denied-directory check on the path text and on the real path.

    A denied directory that is a symlink denies its target as well, so a file
    whose real path lands there is refused even when the path string does not
    name the denied directory.
    """

    if is_denied(path, home):
        return True
    if type(real) is not str:
        return True
    if is_denied(real, home):
        return True
    for denied in denied_paths(home):
        try:
            target = os.path.realpath(denied)
        except (OSError, ValueError):
            continue
        if _nested(real, target):
            return True
    return False


def _nested(path: str, root: str) -> bool:
    if type(path) is not str or type(root) is not str or not root:
        return False
    if path == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path.startswith(prefix)
