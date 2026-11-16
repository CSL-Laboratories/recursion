"""The paths one user's install owns, and the file operations on them.

A root is resolved once (links above it are the administrator's, such as ``/home`` pointing at
``/var/home``). Under a root, every folder this package creates and every file it writes or removes
is reached without following a link: a link there is refused (``symlink_refused`` at a folder,
``not_regular`` at a file), and so is a folder there that another user owns or that group or world
can write (``folder_untrusted``). Nothing
here reads ``os.environ`` or the process's home folder; the caller passes the roots.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
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
# The route homes' custody root: daemon/launcher.py makes each route home at <state>/homes/<engine>
# (its private_dir), and setup.route_home.check refuses a root that is missing or not owner-only, so
# install makes it, 0700, before the first launch (lane INSTALL's question 15).
HOMES_FOLDER = "homes"


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
    homes: str             # <state>/homes, the route homes' custody root (route_home.check's root)


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
        homes=os.path.join(state, HOMES_FOLDER),
    )


def _directory_root(path: object) -> str:
    """The home, resolved, after checking that no other user can change what it names.

    Each folder from ``/`` to the home is resolved as the kernel does (links followed, at most
    ``_MAX_LINKS``): each must be root's or ours and not writable by group or world unless sticky,
    and each link on the way root's or ours (``folder_untrusted``). The home itself must be ours and
    not writable by group or world at all. The rule ``plugin_security.stores`` holds a store's
    folders to.
    """

    if type(path) is not str or not os.path.isabs(path):
        raise InstallRefused("relative_path", "a root is an absolute path")
    trusted = (0, os.geteuid())
    names = [name for name in path.split("/") if name]
    current, links = "/", 0
    _trusted_folder("/", os.lstat("/"), trusted)
    while names:
        candidate = os.path.join(current, names.pop(0))
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            raise InstallRefused("root", f"{path} is not a folder") from None
        except OSError as exc:  # a folder on the way that cannot be searched, an I/O error
            raise InstallRefused("folder_untrusted", f"{candidate}: {type(exc).__name__}") from None
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in trusted:
                raise InstallRefused("folder_untrusted", f"{candidate} is a link another user owns")
            links += 1
            if links > _MAX_LINKS:
                raise InstallRefused("folder_untrusted", f"more than {_MAX_LINKS} links on the way to {path}")
            try:
                target = os.readlink(candidate)
            except OSError as exc:  # removed or replaced since the lstat above
                raise InstallRefused("folder_untrusted", f"{candidate}: {type(exc).__name__}") from None
            if target.startswith("/"):
                current = "/"
            names[:0] = [part for part in target.split("/") if part]
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise InstallRefused("root", f"{path} is not a folder")
        _trusted_folder(candidate, info, trusted)
        current = candidate
    home = os.lstat(current)
    if home.st_uid != os.geteuid():
        raise InstallRefused("folder_untrusted", f"{current} is owned by another user")
    if home.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InstallRefused("folder_untrusted", f"{current} can be written by group or world; `chmod go-w` it")
    return os.path.realpath(path)


_MAX_LINKS = 40  # Linux's MAXSYMLINKS


def _trusted_folder(path: str, info: os.stat_result, trusted: tuple) -> None:
    if info.st_uid not in trusted:
        raise InstallRefused("folder_untrusted", f"{path} is owned by another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
        raise InstallRefused("folder_untrusted", f"{path} is writable by group or world and not sticky")


def ensure_dir(root: str, path: str, *, mode: int = 0o700) -> bool:
    """Create ``path`` and the folders between it and ``root``. True when anything was created.

    Every new folder gets ``mode`` (less the umask). An existing folder is left as it is, if
    ``_existing_folder`` accepts it: a link, a file, or a folder another user owns or can write
    on the way is refused instead of being used.
    """

    created = False
    for cur in _folders_below(root, path):
        if _existing_folder(cur):
            continue
        try:
            os.mkdir(cur, mode)
        except FileExistsError:  # another caller made it since the look above: the same rule holds for it
            _existing_folder(cur)
            continue
        created = True
    return created


def check_dirs(root: str, path: str) -> None:
    """Refuse what ``ensure_dir`` would refuse at any folder between ``root`` and ``path`` that exists."""

    for cur in _folders_below(root, path):
        _existing_folder(cur)


def check_private_dir(root: str, path: str) -> None:
    """``check_dirs``, and ``path`` itself, if it is there, open to nobody else at all (no bit of
    0o077) and usable by its owner (read, write and search): the route homes' custody root, which
    ``setup.route_home.check`` (lane INSTALL's #401, a separate pull request) holds to the first rule
    and the launcher's ``private_dir`` (``daemon/launcher.py``) needs the second for."""

    check_dirs(root, path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if info.st_mode & 0o077:
        raise InstallRefused("folder_untrusted", f"{path} is open to group or world; route homes need it owner-only: `chmod 700` it")
    if (info.st_mode & stat.S_IRWXU) != stat.S_IRWXU:
        raise InstallRefused("folder_untrusted", f"{path} must be readable, writable and searchable by its owner; `chmod 700` it")


def _folders_below(root: str, path: str) -> list[str]:
    rel = os.path.relpath(path, root)
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        raise InstallRefused("path_escapes", path)
    folders, cur = [], root
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        folders.append(cur)
    return folders


def _existing_folder(path: str) -> bool:
    """True for an existing folder this installer may use, False when nothing is there.

    Refused: a link (``symlink_refused``), something that is not a folder (``not_a_directory``), and
    a folder another user owns or that group or world can write (``folder_untrusted``): another user
    could put a unit there, or take ours away. The same rule ``plugin_security.stores`` holds a
    store's folder to.
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise InstallRefused("symlink_refused", path)
    if not stat.S_ISDIR(info.st_mode):
        raise InstallRefused("not_a_directory", path)
    if info.st_uid != os.geteuid():
        raise InstallRefused("folder_untrusted", f"{path} is owned by another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InstallRefused("folder_untrusted", f"{path} can be written by group or world; `chmod go-w` it")
    return True


def read_regular(path: str) -> bytes:
    """Read a regular file of at most 1 MiB. A link is refused, not followed.

    The open does not block (``O_NONBLOCK``): a FIFO or a device in the file's place is refused by
    the type check after it instead of holding the command at the open. Regular files ignore the flag.
    """

    return read_regular_and_stat(path)[0]


def read_regular_and_stat(path: str) -> tuple[bytes, os.stat_result]:
    """``read_regular``, with the ``fstat`` of the descriptor the bytes were read through: the owner
    and mode of the very file that was read. Before that descriptor is closed the path is looked up
    again and must still name the file read (``still_names``): while the descriptor is open its inode
    cannot be given to another file, so the comparison holds (after the close, a file made again at
    the path could take the freed number). A file not there at the open is ``unit_missing`` (the
    installer's code, as for every refusal here), not a file of the wrong type. The ``fstat`` returned
    is for ``file_untrusted``; a caller that must compare the path again uses ``held_regular``."""

    with held_regular(path) as read:
        return read[0], read[1]


@contextlib.contextmanager
def held_regular(path: str):
    """``read_regular_and_stat``'s read, yielding ``(bytes, fstat)`` with the descriptor still open, after
    ``still_names`` has passed. What the caller then does by name inside the block (``uninstall`` removes
    the unit) meets the file read unless another file was put at the name since that lookup: no
    call by name can close that last window, which this keeps to the few statements in the block."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:  # not there (a concurrent uninstall removed it since the caller looked)
        raise InstallRefused("unit_missing", f"{path} is not there; run the command again") from None
    except OSError as exc:
        # A link at the name (O_NOFOLLOW) or a socket there (the non-blocking open takes a FIFO, not a
        # socket) is the file's type; any other error (too many open files, an I/O error) is the error
        # itself, which setup.run answers as filesystem_error with the error and the path.
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            raise InstallRefused("not_regular", path) from exc
        raise
    try:
        info = os.fstat(fd)
        if not _is_reg(info.st_mode):
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
        still_names(path, info)  # while the descriptor is open: the inode is not free to be reused
        read = _Held((b"".join(chunks), info))
        read.fd = fd
        yield read
    finally:
        os.close(fd)


class _Held(tuple):
    """``(bytes, fstat)`` from ``held_regular``, with ``.fd``, the descriptor still open, for ``still_holds``."""

    fd: int


def still_holds(path: str, read: _Held) -> None:
    """Refuse ``unit_changed`` unless the file still held open holds the bytes read from it: its owner can
    rewrite a file in place (the same inode), which ``still_names`` cannot see. Read again through the
    held descriptor from the start, one byte past the length read, so an append shows too."""

    data = read[0]
    if os.pread(read.fd, len(data) + 1, 0) != data:
        raise InstallRefused("unit_changed", f"{path} was rewritten while it was read; run the command again")


def still_names(path: str, info: os.stat_result) -> None:
    """Refuse unless ``path`` still names the file ``info`` describes (the same device and inode): removed
    meanwhile (a concurrent ``uninstall``) is ``unit_missing``, replaced is ``unit_changed``; nothing is
    written either way, and the command run again reads what is there then. Sound only while a
    descriptor of that file is open, which is why ``held_regular`` calls it before the close."""

    try:
        now = os.lstat(path)
    except FileNotFoundError:
        raise InstallRefused("unit_missing", f"{path} was removed while it was read; run the command again") from None
    if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
        raise InstallRefused("unit_changed", f"{path} was replaced while it was read; run the command again")


def file_untrusted(path: str, info: os.stat_result) -> str:
    """Why another user could change the file ``info`` describes (take it from
    ``read_regular_and_stat``, so it is the file that was read; ``path`` only names it): it is
    another user's, or group or world can write it. ``""`` when only we can. The folder rule above,
    held for a file this package accepts as it is (the unit ``install`` finds already in place). Owner
    and mode bits only: POSIX ACLs are not read, as for the folders and the stores. No lookup, so it
    raises nothing."""

    if info.st_uid != os.geteuid():
        return f"{path} is owned by another user"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path} can be written by group or world"
    return ""


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
    """Replace a regular file with ``data``. One lookup decides: nothing there (a concurrent
    ``uninstall``) is ``unit_missing``; a link (refused, not followed) or anything but a regular file
    is ``not_regular``; a regular file is replaced (one removed after the lookup is written anew)."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise InstallRefused("unit_missing", f"{path} is not there; run the command again") from None
    if not stat.S_ISREG(info.st_mode):
        raise InstallRefused("not_regular", path)
    tmp = _write_temp(path, data, mode)
    try:
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    _fsync_dir(os.path.dirname(path))


def unlink_regular(path: str) -> bool:
    """Remove the regular file at ``path``. Missing is not an error (False), also when another caller
    removed it between this function's one lookup and its unlink; a link or folder is refused."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise InstallRefused("not_regular", path)
    try:
        os.unlink(path)
    except FileNotFoundError:  # removed by another caller since the lookup above
        return False
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
