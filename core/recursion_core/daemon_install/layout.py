"""The paths one user's install owns, and the file operations on them.

A root is resolved once (links above it are the administrator's, such as ``/home`` pointing at
``/var/home``). Under a root, every folder this package creates and every file it writes or removes
is reached without following a link: a link there is refused (``symlink_refused``). Nothing here
reads ``os.environ`` or the process's home folder; the caller passes the roots.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from .errors import InstallRefused
from .unit import STATE_HOME, UNIT_NAME

# Where the daemon opens its stores, under its state folder (tests/test_daemon_install.py holds these
# to the daemon's own names): daemon/__main__.py's "recursion", daemon/breaker.py's STORE_DIR and
# STORE_FILE, daemon/plugins.py's REVOCATION_FILE.
STATE_FOLDER = "recursion"
STORE_FOLDER = "plugin-security"
BREAKER_FILE = "breaker.sqlite3"
REVOCATION_FILE = "revoked.sqlite3"


@dataclass(frozen=True)
class Roots:
    """``home`` is the user's home folder as the user manager sees it (``%h``)."""

    home: str


@dataclass(frozen=True)
class Owned:
    home: str              # the resolved root
    unit_dir: str          # <home>/.config/systemd/user
    unit: str              # <unit_dir>/recursion-daemon.service
    enabled: str           # <unit_dir>/default.target.wants/recursion-daemon.service, made by `enable`
    state: str             # <home>/.local/state/recursion, the daemon's state folder under the unit
    store_dir: str         # <state>/plugin-security
    breaker_store: str     # <store_dir>/breaker.sqlite3
    revocation_store: str  # <store_dir>/revoked.sqlite3


def owned_paths(roots: Roots) -> Owned:
    if type(roots) is not Roots:
        raise InstallRefused("roots")
    home = _directory_root(roots.home)
    unit_dir = os.path.join(home, ".config", "systemd", "user")
    state = os.path.join(home, *STATE_HOME.split("/"), STATE_FOLDER)
    store_dir = os.path.join(state, STORE_FOLDER)
    return Owned(
        home=home,
        unit_dir=unit_dir,
        unit=os.path.join(unit_dir, UNIT_NAME),
        enabled=os.path.join(unit_dir, "default.target.wants", UNIT_NAME),
        state=state,
        store_dir=store_dir,
        breaker_store=os.path.join(store_dir, BREAKER_FILE),
        revocation_store=os.path.join(store_dir, REVOCATION_FILE),
    )


def _directory_root(path: object) -> str:
    if type(path) is not str or not os.path.isabs(path):
        raise InstallRefused("relative_path", "a root is an absolute path")
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise InstallRefused("root", f"{path} is not a folder")
    return real


def ensure_dir(root: str, path: str, *, mode: int = 0o700) -> bool:
    """Create ``path`` and the folders between it and ``root``. True when anything was created.

    Every new folder gets ``mode`` (less the umask). An existing folder is left as it is. A link
    or a file on the way is refused instead of being followed.
    """

    rel = os.path.relpath(path, root)
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        raise InstallRefused("path_escapes", path)
    created = False
    cur = root
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise InstallRefused("symlink_refused", cur)
        if os.path.isdir(cur):
            continue
        if os.path.lexists(cur):
            raise InstallRefused("not_a_directory", cur)
        os.mkdir(cur, mode)
        created = True
    return created


def check_dirs(root: str, path: str) -> None:
    """Refuse a link or a file at any folder between ``root`` and ``path`` that exists."""

    rel = os.path.relpath(path, root)
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        raise InstallRefused("path_escapes", path)
    cur = root
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise InstallRefused("symlink_refused", cur)
        if os.path.lexists(cur) and not os.path.isdir(cur):
            raise InstallRefused("not_a_directory", cur)


def read_regular(path: str) -> bytes:
    """Read a regular file of at most 1 MiB. A link is refused, not followed."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise InstallRefused("not_regular", path) from exc
    try:
        if not _is_reg(os.fstat(fd).st_mode):
            raise InstallRefused("not_regular", path)
        chunks, size = [], 0
        while True:
            block = os.read(fd, 1 << 16)
            if not block:
                break
            size += len(block)
            if size > 1 << 20:
                raise InstallRefused("too_large", path)
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_exclusive(path: str, data: bytes, mode: int) -> None:
    """Create ``path`` as a new regular file holding ``data``. Anything already there is refused."""

    tmp = _write_temp(path, data, mode)
    try:
        os.link(tmp, path, follow_symlinks=False)
    except FileExistsError:
        raise InstallRefused("already_present", path) from None
    finally:
        os.unlink(tmp)
    _fsync_dir(os.path.dirname(path))


def replace_regular(path: str, data: bytes, mode: int) -> None:
    """Replace a regular file with ``data``. A link at ``path`` is refused, not followed."""

    if os.path.islink(path) or not os.path.isfile(path):
        raise InstallRefused("not_regular", path)
    tmp = _write_temp(path, data, mode)
    try:
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    _fsync_dir(os.path.dirname(path))


def unlink_regular(path: str) -> bool:
    """Remove the regular file at ``path``. Missing is not an error (False); a link or folder is refused."""

    if not os.path.lexists(path):
        return False
    if os.path.islink(path) or not os.path.isfile(path):
        raise InstallRefused("not_regular", path)
    os.unlink(path)
    _fsync_dir(os.path.dirname(path))
    return True


def _write_temp(path: str, data: bytes, mode: int) -> str:
    """Write ``data`` to a new file with a fresh name beside ``path``; fsynced, mode exactly ``mode``."""

    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    return tmp


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_reg(mode: int) -> bool:
    return (mode & 0o170000) == 0o100000
