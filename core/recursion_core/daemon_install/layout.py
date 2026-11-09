"""Owned paths for one user's daemon install (CSL-758).

Paths are lexical and stay under the root the caller passed. A symlink at a
root, or at any directory this package would create or remove, is refused.
Nothing here looks at ``os.environ`` or the process home directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import InstallRefused
from .unit import UNIT_NAME


@dataclass(frozen=True)
class Roots:
    """Absolute real directories. ``home`` holds the user unit. ``runtime_dir``
    holds the socket path uninstall clears. ``state_dir`` holds the grant
    pointer and the registry directory upgrade and uninstall leave in place.
    """

    home: str
    runtime_dir: str
    state_dir: str


@dataclass(frozen=True)
class Owned:
    unit: str
    socket: str
    grant: str
    registry: str


def owned_paths(roots: Roots) -> Owned:
    if type(roots) is not Roots:
        raise InstallRefused("roots")
    _directory_root(roots.home)
    _directory_root(roots.runtime_dir)
    _directory_root(roots.state_dir)
    return Owned(
        unit=_under(roots.home, os.path.join(roots.home, ".config", "systemd", "user", UNIT_NAME)),
        socket=_under(roots.runtime_dir, os.path.join(roots.runtime_dir, "recursion-daemon.sock")),
        grant=_under(roots.state_dir, os.path.join(roots.state_dir, "grants", "engine-user.json")),
        registry=_under(roots.state_dir, os.path.join(roots.state_dir, "registry")),
    )


def _directory_root(path: str) -> str:
    if type(path) is not str or not os.path.isabs(path):
        raise InstallRefused("relative_path")
    if os.path.islink(path):
        raise InstallRefused("symlink_refused")
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise InstallRefused("root")
    # A root whose lexical path is not its real path has a symlink in a parent.
    # Refuse it so later joins cannot leave the directory the caller named.
    if os.path.normpath(path) != real:
        raise InstallRefused("symlink_refused")
    return real


def _under(root: str, path: str) -> str:
    norm_root = os.path.normpath(root)
    norm_path = os.path.normpath(path)
    if os.path.commonpath([norm_root, norm_path]) != norm_root:
        raise InstallRefused("path_escapes")
    rel = os.path.relpath(norm_path, norm_root)
    if rel.startswith("..") or rel == ".":
        raise InstallRefused("path_escapes")
    return norm_path


def ensure_dir(root: str, path: str, *, mode: int = 0o700) -> None:
    """Create ``path`` under ``root``. Existing directories stay as they are.

    Every new directory is ``mode``. An existing symlink component is refused
    instead of being followed. ``path`` equal to ``root`` is a no-op.
    """

    norm_root = _directory_root(root)
    if os.path.normpath(path) == os.path.normpath(root):
        return
    norm_path = _under(root, path)
    rel = os.path.relpath(norm_path, norm_root)
    cur = norm_root
    for part in rel.split(os.sep):
        if part in ("", ".", ".."):
            raise InstallRefused("path_escapes")
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise InstallRefused("symlink_refused")
        if os.path.isdir(cur):
            continue
        if os.path.lexists(cur):
            raise InstallRefused("not_a_directory")
        os.mkdir(cur, mode)


def read_regular(path: str) -> bytes:
    """Read a regular file. A symlink is refused, not followed."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise InstallRefused("not_regular") from exc
    try:
        st = os.fstat(fd)
        if not _is_reg(st.st_mode):
            raise InstallRefused("not_regular")
        chunks = []
        while True:
            block = os.read(fd, 1 << 16)
            if not block:
                break
            chunks.append(block)
            if sum(len(c) for c in chunks) > 1 << 20:
                raise InstallRefused("too_large")
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_exclusive(path: str, data: bytes, mode: int) -> None:
    """Create ``path`` as a new regular file. An existing path is refused."""

    if os.path.lexists(path):
        raise InstallRefused("already_present")
    _write_temp_and_replace(path, data, mode, exclusive_dest=True)


def replace_regular(path: str, data: bytes, mode: int) -> None:
    """Replace a regular file. A symlink at ``path`` is refused, not followed."""

    if os.path.islink(path) or not os.path.isfile(path):
        raise InstallRefused("not_regular")
    _write_temp_and_replace(path, data, mode, exclusive_dest=False)


def unlink_owned(root: str, path: str) -> bool:
    """Unlink ``path`` when it is under ``root``. Missing is not an error.

    Parent directories must not be symlinks. Unlinking a symlink removes the
    symlink itself, not its target.
    """

    norm_path = _under(root, path)
    parent = os.path.dirname(norm_path)
    if os.path.islink(parent):
        raise InstallRefused("symlink_refused")
    parent_real = os.path.realpath(parent)
    root_real = _directory_root(root)
    if os.path.commonpath([root_real, parent_real]) != root_real:
        raise InstallRefused("path_escapes")
    if not os.path.lexists(norm_path):
        return False
    os.unlink(norm_path)
    return True


def _write_temp_and_replace(path: str, data: bytes, mode: int, *, exclusive_dest: bool) -> None:
    parent = os.path.dirname(path)
    if os.path.islink(parent):
        raise InstallRefused("symlink_refused")
    tmp = os.path.join(parent, "." + os.path.basename(path) + ".tmp")
    if os.path.lexists(tmp):
        raise InstallRefused("temp_exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            wrote = os.write(fd, view)
            if wrote <= 0:
                raise InstallRefused("short_write")
            view = view[wrote:]
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    if exclusive_dest and os.path.lexists(path):
        os.unlink(tmp)
        raise InstallRefused("already_present")
    os.replace(tmp, path)


def _is_reg(mode: int) -> bool:
    return (mode & 0o170000) == 0o100000
