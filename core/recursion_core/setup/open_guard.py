"""One gate for reading a settings file. ``observe`` and ``import`` share it.

The containment check is #564's ``_inside``: the real path must stay strictly
inside the named root. The denied-directory check uses that same real path,
including when the denied directory itself is a symlink. A file is read only
when lstat before the open and fstat on the same descriptor both say one link
and the same inode. Otherwise no byte is returned.

``O_NOFOLLOW`` is included when the interpreter has that name. When the name is
absent the one-link and same-inode checks still run before any byte is returned.
A file whose link count cannot be read is refused. The check is not skipped.
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


def _nofollow() -> int:
    """The no-follow flag when this interpreter has it. Never a substitute for the link check."""

    flag = getattr(os, "O_NOFOLLOW", 0)
    return flag if isinstance(flag, int) else 0


def read_regular_file(path: str, *, root: str, home: str, max_bytes: int, denied_ids=None) -> bytes:
    """Return the file bytes, or raise ``GateRefused`` without a useful payload.

    ``denied_ids`` is the optional inode set of denied paths. It does not replace
    the one-link rule: a hard link is refused because its link count is not one.
    The link count and the inode identity are checked even when ``O_NOFOLLOW`` is
    absent. A missing flag does not skip them, and no byte is returned on a miss.
    """

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
    if not inside_root(path, root):
        raise GateRefused("outside")
    if _real_denied(path, home) or (denied_ids and (info.st_dev, info.st_ino) in denied_ids):
        raise GateRefused("denied")
    if (info.st_mode & 0o400) == 0 or info.st_size > max_bytes:
        raise GateRefused("unreadable")
    flags = os.O_RDONLY | os.O_CLOEXEC | _nofollow()
    try:
        fd = os.open(path, flags)
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


def inside_root(path: str, root: str) -> bool:
    """True when the real path of ``path`` is strictly inside the real path of ``root``.

    An ancestor symlink that leaves ``root`` is false. This is #564's ``_inside``.
    """

    try:
        real_root = os.path.realpath(root)
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    if type(real) is not str or type(real_root) is not str or not real_root:
        return False
    if real == real_root:
        return False
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    return real.startswith(prefix)


def _real_denied(path: str, home: str) -> bool:
    """Denied-directory check on the path text and on the real path.

    A denied directory that is a symlink denies its target as well, so a file
    whose real path lands there is refused even when the path string does not
    name the denied directory.
    """

    if is_denied(path, home):
        return True
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
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
