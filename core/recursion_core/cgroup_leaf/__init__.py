"""Same-uid cgroup v2 leaf launch with kill-tree on every exit path (CSL-728).

The launcher creates a leaf under an operator-owned, delegated cgroup v2 root and
starts the child through a trampoline that joins the leaf before it execs the target,
so no target code runs outside the leaf. Every exit path (normal return, exception,
launch failure, cancellation) kills the leaf with ``cgroup.kill`` and books
``leaf_empty`` only on positive evidence: ``populated 0`` and the main process's exit
seen through its pidfd and ``waitpid``. Anything else is ``running`` (populated) or
``unknown``.

This launcher never books ``terminated``. A same-uid process can leave the leaf
unseen, and the leaf then reads empty while it lives, so an empty leaf is not proof
that the tree ended (PRODUCT.md 055cc7a5 section 3.4 S5 and the Stop bullet, C1-27:
never ``terminated`` for a tree with an unobservable escapee). ``terminated`` is
reserved for a launch path that can observe escapes; every outcome here carries
``escape_observable=False``.

Same uid only, and no cross-uid kill path. A process of the same uid can leave the
leaf, for example through ``systemd-run --user`` into its user manager, so this is not
a containment or security boundary. The later cross-uid path must require that the
engine user has no user manager during runs (no linger, no running user@ service):
engine linger plus an operator leaf is not containment (Lucas, 2026-11-07, on B6 and B9).
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Leaf ids are run-[A-Za-z0-9_-]{1,64}. The dot in a systemd run-*.scope fails
# this. Another tool can still use run-<id>, so the name is not ownership.
LEAF_NAME = re.compile(r'run-[a-zA-Z0-9_-]{1,64}')
OWNERSHIP_XATTR = 'user.recursion.leaf'
OWNERSHIP_VALUE = b'recursion-cgroup-leaf/1'
CLEANUP_ATTEMPTS = 5  # bounded: an interrupt storm leaves the leaf to the sweep
MAX_EBUSY_ATTEMPTS = 32  # dying-task rmdir retries, also cut by the cleanup deadline
_READ_CAP = 1 << 20
_MAX_CGROUP_DEPTH = 64


def mark_owned(path):
    """Record that this launcher created ``path``.

    The mark sits on the directory inode. Sweep and adopt trust a directory
    only when its name is a leaf id and this attribute is present. A shared
    delegated root can hold a systemd ``run-*.scope`` or another tool's
    ``run-<id>``; the ``run-`` prefix alone is not ownership, and sweep does
    not kill from the name.
    """
    os.setxattr(os.fspath(path), OWNERSHIP_XATTR, OWNERSHIP_VALUE, follow_symlinks=False)


def _marked_owned(target):
    """True only when ``target`` (path or dirfd) carries the marker we write.

    A dirfd is read with ``fgetxattr`` (no ``follow_symlinks``: CPython rejects
    that pair, and the fd was opened with ``O_NOFOLLOW``). A path uses
    ``lgetxattr`` so a symlink cannot borrow the target's marker.

    ``ENODATA`` means the attribute is absent: the target is unmarked.
    ``EOPNOTSUPP`` and every other ``OSError`` are raised. Those are not
    "unmarked"; the caller must report them and must not kill the directory.
    """
    if target is None:
        return False
    try:
        if isinstance(target, int):
            value = os.getxattr(target, OWNERSHIP_XATTR)
        else:
            value = os.getxattr(os.fspath(target), OWNERSHIP_XATTR, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return False
        raise
    return value == OWNERSHIP_VALUE


def _discard_new_leaf(leaf):
    """Best-effort undo of a leaf whose ownership marker could not be written."""
    leaf = Path(leaf)
    try:
        for entry in leaf.iterdir():
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                entry.unlink()
            except OSError:
                pass
    except OSError:
        pass
    try:
        os.rmdir(leaf)
    except OSError:
        pass


def cgroup_identity(path):
    """``(st_dev, st_ino)`` of a real directory, or None when the path is not one."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


# vfs_rmdir unhashes the dentry, and d_path then appends this suffix.
_DELETED_SUFFIX = ' (deleted)'


def _directory_entry_inode(parent_fd, name):
    """``(st_dev, st_ino)`` when ``name`` in ``parent_fd`` is that directory.

    ``False`` when the name is missing or is not a directory (a symlink is
    not the directory inode). ``None`` when the lookup itself failed.
    """
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    return (info.st_dev, info.st_ino)


def _parent_contains_inode(parent_fd, identity):
    """True when any entry of ``parent_fd`` is ``identity``.

    ``False`` when the scan finishes without it. ``None`` when the scan
    cannot be trusted (a lookup failed with something other than ``ENOENT``).
    """
    try:
        names = os.listdir(parent_fd)
    except OSError:
        return None
    for name in names:
        found = _directory_entry_inode(parent_fd, name)
        if found is None:
            return None
        if found == identity:
            return True
    return False


def _held_fd_unlinked(held, info):
    """Whether the held directory inode is unlinked.

    ``True`` or ``False`` when that is known. ``None`` when it is not.

    tmpfs and the other POSIX filesystems drop ``st_nlink`` to 0 while an fd
    is still open. Linux 6.12 cgroupfs does not. ``kernfs_iop_getattr`` calls
    ``kernfs_refresh_inode``, which sets a directory nlink to ``subdirs + 2``
    on every getattr (``fs/kernfs/inode.c`` in v6.12, no ``KERNFS_REMOVING``
    guard). ``__kernfs_remove`` deactivates the node and does not call
    ``clear_nlink``; that clear landed later, with the 2026 kernfs inotify
    series, and is not in 6.12. ``fstat`` of a removed cgroup directory
    therefore still reports nlink >= 2.

    ``vfs_rmdir`` still ``d_delete``s the dentry on success, so
    ``/proc/self/fd/<fd>`` gains a `` (deleted)`` suffix. A rename does not:
    the fd follows the inode to its new name. A path whose real name ends
    with that suffix is still linked, and ``lstat`` of the readlink target
    finds it; the kernel suffix is only trusted when that full string is not
    this inode.
    """
    if info.st_nlink == 0:
        return True
    try:
        link = os.readlink(f'/proc/self/fd/{held}')
    except OSError:
        return None
    if not link.endswith(_DELETED_SUFFIX):
        return False
    try:
        named = os.lstat(link)
    except FileNotFoundError:
        named = None
    except OSError:
        return None
    if (named is not None and stat.S_ISDIR(named.st_mode)
            and (named.st_dev, named.st_ino) == (info.st_dev, info.st_ino)):
        return False
    stripped = link[:-len(_DELETED_SUFFIX)]
    try:
        still = os.lstat(stripped)
    except FileNotFoundError:
        return True
    except OSError:
        return None
    if stat.S_ISDIR(still.st_mode) and (still.st_dev, still.st_ino) == (info.st_dev, info.st_ino):
        return False
    return True


def _held_procs_state(held):
    """How ``cgroup.procs`` looks through the held directory fd.

    ``live`` when the open succeeds. ``dead`` on ``ENODEV``: Linux 6.12
    kernfs returns that from ``kernfs_get_active`` once ``__kernfs_remove``
    has deactivated the node. ``absent`` on ``ENOENT``: the name is gone,
    which is also what the tmpfs test hook produces by unlinking interface
    files before ``rmdir`` while the directory inode is still linked.
    ``unknown`` on any other error. ``absent`` is not, by itself, proof.
    """
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        probe = os.open('cgroup.procs', flags, dir_fd=held)
    except OSError as exc:
        if exc.errno == errno.ENODEV:
            return 'dead'
        if exc.errno == errno.ENOENT:
            return 'absent'
        return 'unknown'
    try:
        os.close(probe)
    except OSError:
        pass
    return 'live'


def _held_directory_removed(parent_fd, name, held, identity):
    """True only when ``held`` is positively shown to be the removed inode.

    The name in the parent must no longer be this inode, and no remaining
    parent entry may be it (a rename aside, including to ``escaped.scope``,
    stays visible). The inode is then removed when any of these hold:

    * ``st_nlink == 0`` (tmpfs and any filesystem that keeps a real link count)
    * the held fd is unlinked (`` (deleted)``) and ``cgroup.procs`` is not
      still openable
    * ``open`` of ``cgroup.procs`` fails with ``ENODEV``, the 6.12 kernfs
      signal that the node is deactivated, even though nlink stays >= 2

    ``ENOENT`` from that open is not enough on its own: the tmpfs test hook
    unlinks interface files before ``rmdir``, so a directory that was only
    renamed aside looks the same until the parent scan or the link count
    says it is still there. Anything that cannot be shown returns False.
    The caller must not book the directory removed and must not drop the fd.
    """
    try:
        info = os.fstat(held)
    except OSError:
        return False
    identity = tuple(identity)
    if (info.st_dev, info.st_ino) != identity or not stat.S_ISDIR(info.st_mode):
        return False
    if parent_fd is None:
        # No parent to scan. nlink 0 is enough; otherwise only ENODEV
        # (a deactivated kernfs node) proves the inode is not linked elsewhere.
        if info.st_nlink != 0 and _held_procs_state(held) != 'dead':
            return False
    else:
        named = _directory_entry_inode(parent_fd, name)
        if named is None or named == identity:
            return False
        present = _parent_contains_inode(parent_fd, identity)
        if present is None or present is True:
            return False
    unlinked = _held_fd_unlinked(held, info)
    procs = _held_procs_state(held)
    if info.st_nlink == 0 and unlinked is True:
        return True
    if procs == 'dead':
        return True
    if unlinked is True and procs == 'absent':
        return True
    return False


def _wait_fd(fd, write, deadline):
    """Wait until ``fd`` is ready, or until ``deadline``. None means do not block."""
    if deadline is None:
        timeout = 0
    else:
        timeout = deadline - time.monotonic()
        if timeout < 0:
            timeout = 0
    readable, writable, _ = select.select([] if write else [fd], [fd] if write else [], [], timeout)
    return bool(writable if write else readable)

# Runs in the child: join the leaf, then exec. On any failure it reports through
# the error pipe and exits without exec, so the target never runs outside the leaf.
TRAMPOLINE = '''import os, sys
procs, err = int(sys.argv[1]), int(sys.argv[2])
try:
    os.write(procs, str(os.getpid()).encode())
    os.close(procs)
    os.set_inheritable(err, False)
    os.execvp(sys.argv[3], sys.argv[3:])
except BaseException as exc:
    os.write(err, repr(exc).encode()[:4000] or b'?')
    os._exit(127)
'''


class LaunchError(RuntimeError):
    """The target did not start inside the leaf."""


class CleanupIncomplete(RuntimeError):
    def __init__(self, outcome):
        super().__init__(f'leaf cleanup incomplete: {outcome}')
        self.outcome = outcome


@dataclass(frozen=True)
class Outcome:
    state: str  # leaf_empty | empty | running | unknown; never terminated (S5, C1-27)
    returncode: int | None
    populated: bool | None
    main_exit_seen: bool
    errors: tuple[str, ...] = ()
    escape_observable: bool = False  # same uid: a process that left the leaf is unseen


@dataclass(frozen=True)
class SweepResult:
    """``outcomes`` are leaves this launcher killed. ``reported`` were left in place.

    Each report is ``(name, reason)``. Reasons are ``foreign-name``, ``unmarked``,
    ``symlink``, ``unreadable`` and ``marker-unreadable:<errno>``. ``ENODATA``
    is ``unmarked``: the attribute is absent. ``EOPNOTSUPP`` and any other
    error while reading the marker are ``marker-unreadable`` with that errno.
    A report is not a kill and not a removal.
    """
    outcomes: dict
    reported: tuple


class Cgroup2:
    """cgroup v2 filesystem operations. Tests substitute a fake."""
    def __init__(self, mount=Path('/sys/fs/cgroup')):
        self.mount = Path(os.path.realpath(mount))

    def make_leaf(self, leaf: Path):
        os.mkdir(leaf, 0o755)  # exclusive: an existing leaf is never reused
        try:
            mark_owned(leaf)
        except OSError:
            _discard_new_leaf(leaf)
            raise

    def open_procs(self, leaf: Path) -> int:
        return os.open(leaf / 'cgroup.procs', os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)

    def kill(self, leaf: Path, deadline=None, dirfd=None):
        # O_NONBLOCK: a fifo or a stuck node must not hold the cleanup window open.
        # dirfd pins the directory this handle opened. A replaced path is not written.
        flags = os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        if dirfd is None:
            fd = os.open(Path(leaf) / 'cgroup.kill', flags)
        else:
            fd = os.open('cgroup.kill', flags, dir_fd=dirfd)
        try:
            if not _wait_fd(fd, True, deadline):
                raise OSError(errno.ETIMEDOUT, 'cgroup.kill not ready before deadline')
            os.write(fd, b'1')
        finally:
            os.close(fd)

    def _read_text(self, path, deadline, dirfd=None, name=None):
        """Read a cgroup file without blocking past ``deadline``. None if it cannot."""
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            if dirfd is None:
                fd = os.open(path, flags)
            else:
                fd = os.open(name, flags, dir_fd=dirfd)
        except OSError:
            return None
        try:
            chunks, size = [], 0
            while size <= _READ_CAP:
                if not _wait_fd(fd, False, deadline):
                    return None
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    # BlockingIOError, and also ENODEV or EIO while the cgroup
                    # is going away. The caller treats None as "could not read".
                    return None
                if not chunk:
                    return b''.join(chunks).decode(errors='replace')
                chunks.append(chunk)
                size += len(chunk)
            return None
        finally:
            os.close(fd)

    def populated(self, leaf: Path, deadline=None, dirfd=None) -> bool | None:
        text = self._read_text(Path(leaf) / 'cgroup.events', deadline,
                               dirfd=dirfd, name='cgroup.events')
        if not text:
            return None
        try:
            value = dict(line.split() for line in text.splitlines() if line.strip()).get('populated')
        except ValueError:
            return None
        return {'0': False, '1': True}.get(value)

    def _child_cgroups(self, directory, deadline):
        """Child directories and the inode seen at the scan, or None if unsafe.

        None means the listing was incomplete or contained a symlink. The inode
        is what removal must still find: a path reused after the scan is not removed.
        """
        children = []
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if time.monotonic() >= deadline:
                        return None
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        return None
                    if stat.S_ISLNK(info.st_mode):
                        return None  # do not follow or delete through a link
                    if stat.S_ISDIR(info.st_mode):
                        children.append((Path(entry.path), (info.st_dev, info.st_ino)))
        except OSError:
            return None
        return children

    def _procs_empty(self, directory, deadline):
        """True only when procs and threads were read and list no ids."""
        directory = Path(directory)
        for name in ('cgroup.procs', 'cgroup.threads'):
            path = directory / name
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                if name == 'cgroup.procs':
                    return False
                continue
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return False
            text = self._read_text(path, deadline)
            if text is None:
                return False
            for line in text.splitlines():
                if line.strip():
                    return False
        return True

    def _confirmed_empty(self, directory, deadline):
        children = self._child_cgroups(directory, deadline)
        if children is None or children:
            return False
        return self._procs_empty(directory, deadline)

    def _identity_ok(self, directory, identity):
        if identity is None:
            return False
        return cgroup_identity(directory) == identity

    def member(self, pid: int) -> Path | None:
        """The cgroup v2 directory holding pid, or None when unreadable."""
        try:
            for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines():
                if line.startswith('0::/'):
                    return self.mount / line[4:].lstrip('/')
        except OSError:
            pass
        return None

    def remove(self, leaf: Path, deadline=None, identity=None, dirfd=None):
        """rmdir ``leaf`` bottom-up only when it is still ``identity`` and empty.

        ``identity`` is the ``(st_dev, st_ino)`` from when the handle opened the
        directory. None means the caller does not know which cgroup is at this
        path, and nothing is removed. A directory is empty when ``cgroup.procs``
        and ``cgroup.threads`` (if present) list no ids and it has no child
        cgroups and no symlinks. ``rmdir`` is not called before that. ``EBUSY``
        while the directory is no longer empty is returned immediately. ``EBUSY``
        while it still reads empty is the dying-task race and is retried until
        ``deadline`` (or a short bound when the caller did not pass one).
        Interface files are never unlinked; on cgroupfs, ``rmdir`` drops them.
        ``dirfd`` is that same inode when the caller still holds it. ``rmdir``
        uses the parent directory fd. The inode is booked gone only when
        ``_held_directory_removed`` can show it: the parent name is not this
        inode, the parent has no entry for it, and the inode is unlinked.
        ``st_nlink == 0`` is that proof on tmpfs. On Linux 6.12 cgroupfs,
        ``fstat`` still reports nlink >= 2 after a successful ``rmdir``; the
        unlinked dentry and a failed ``open`` of ``cgroup.procs`` through the
        held fd are the proof instead. If the inode is still linked, or that
        cannot be shown, this raises. A name swapped into the parent in the
        gap before ``rmdir`` can still be deleted; that race is not closed.
        """
        if identity is None or cgroup_identity(leaf) != identity:
            raise OSError(errno.EBUSY, 'cgroup identity unknown or changed')
        if deadline is None:
            deadline = time.monotonic() + 0.25
        self._remove_dir(Path(leaf), deadline, identity, 0, dirfd)

    def _remove_dir(self, directory, deadline, identity, depth, dirfd=None):
        if depth > _MAX_CGROUP_DEPTH:
            raise OSError(errno.EBUSY, 'cgroup tree too deep')
        if not self._identity_ok(directory, identity):
            raise OSError(errno.EBUSY, 'cgroup identity changed')
        children = self._child_cgroups(directory, deadline)
        if children is None:
            raise OSError(errno.EBUSY, 'cgroup not confirmed empty')
        for child, child_identity in children:
            if not self._identity_ok(directory, identity):
                raise OSError(errno.EBUSY, 'cgroup identity changed')
            self._remove_dir(child, deadline, child_identity, depth + 1)
        self._rmdir_when_empty(directory, deadline, identity, dirfd)

    def _rmdir_when_empty(self, directory, deadline, identity, dirfd=None):
        attempts = 0
        while True:
            if not self._identity_ok(directory, identity):
                raise OSError(errno.EBUSY, 'cgroup identity changed')
            if not self._confirmed_empty(directory, deadline):
                raise OSError(errno.EBUSY, 'cgroup not empty')
            # The empty read used the path. A directory swapped in after that
            # read must not be the one rmdir removes.
            if not self._identity_ok(directory, identity):
                raise OSError(errno.EBUSY, 'cgroup identity changed')
            try:
                self._rmdir(directory, identity=identity, dirfd=dirfd)
                return
            except FileNotFoundError:
                raise OSError(errno.EBUSY, 'cgroup identity could not be verified') from None
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                # An identity miss is not the dying-task race.
                if exc.strerror and 'identity' in exc.strerror:
                    raise
                attempts += 1
                if not self._confirmed_empty(directory, deadline):
                    raise
                if attempts >= MAX_EBUSY_ATTEMPTS or time.monotonic() >= deadline:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))

    def _prepare_rmdir(self, dirfd):
        """Test hook. On cgroupfs, ``rmdir`` drops interface files itself."""
        return

    def _rmdir(self, directory, identity=None, dirfd=None):
        """``rmdir`` the name in its parent, then require ``identity`` to be gone.

        ``dirfd``, when the caller still holds it, is that inode. The name is
        opened through the parent fd and must be the same inode before
        ``rmdir``. Afterwards ``_held_directory_removed`` must be true.
        ``st_nlink == 0`` is enough on tmpfs. On Linux 6.12 cgroupfs it is
        not: getattr keeps reporting ``subdirs + 2``. Anything short of the
        removal proof raises, and the caller must not book the directory
        removed.
        """
        directory = Path(directory)
        if identity is None:
            raise OSError(errno.EBUSY, 'cgroup identity unknown or changed')
        parent_fd = os.open(directory.parent,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        held = dirfd
        close_held = False
        try:
            if held is None:
                try:
                    held = os.open(directory.name,
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                                   dir_fd=parent_fd)
                except FileNotFoundError:
                    raise OSError(errno.EBUSY, 'cgroup identity could not be verified') from None
                close_held = True
            try:
                info = os.fstat(held)
            except OSError as exc:
                raise OSError(errno.EBUSY, 'cgroup identity could not be verified') from exc
            if ((info.st_dev, info.st_ino) != tuple(identity)
                    or not stat.S_ISDIR(info.st_mode)):
                raise OSError(errno.EBUSY, 'cgroup identity changed')
            try:
                probe = os.open(directory.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                                dir_fd=parent_fd)
            except FileNotFoundError:
                probe = None
            if probe is not None:
                try:
                    named = os.fstat(probe)
                finally:
                    os.close(probe)
                if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                    raise OSError(errno.EBUSY, 'cgroup identity changed')
                self._prepare_rmdir(held)
                try:
                    os.rmdir(directory.name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if not _held_directory_removed(parent_fd, directory.name, held, identity):
                raise OSError(errno.EBUSY, 'cgroup identity still present after rmdir')
        finally:
            os.close(parent_fd)
            if close_held and held is not None:
                os.close(held)


class DelegatedRoot:
    """An operator-owned cgroup v2 directory delegated to this uid.

    The launcher process must itself live inside this subtree: cgroup v2 only lets an
    unprivileged writer migrate a process when it can write the common ancestor's
    cgroup.procs.
    """
    def __init__(self, path, fs: Cgroup2 | None = None):
        self.fs = fs or Cgroup2()
        path = Path(path)
        real = Path(os.path.realpath(path))
        if not path.is_absolute() or real != path:
            raise PermissionError('Delegated root must be an absolute path without symlinks')
        if real == self.fs.mount or not real.is_relative_to(self.fs.mount):
            raise PermissionError('Delegated root must be below the cgroup v2 mount')
        info = os.lstat(real)
        procs = os.lstat(real / 'cgroup.procs')
        if info.st_uid != os.getuid() or procs.st_uid != os.getuid():
            raise PermissionError('Delegated root is not owned by this uid (same uid only)')
        if not (real / 'cgroup.kill').exists():
            raise PermissionError('cgroup.kill missing: no kill path, refusing to launch')
        self.path = real

    def leaf(self, name: str) -> Path:
        if not isinstance(name, str) or not LEAF_NAME.fullmatch(name):
            raise ValueError('Leaf name must match run-[A-Za-z0-9_-]{1,64}')
        return self.path / name

    def launch(self, name, argv, *, env=None, cwd=None, stdin=None, stdout=None,
               stderr=None, start_timeout=30.0, kill_timeout=5.0):
        """Start argv inside a new leaf. Returns a LeafRun; use it as a context manager."""
        if not argv:
            raise ValueError('Empty launch')
        leaf = self.leaf(name)
        self.fs.make_leaf(leaf)
        run = LeafRun(self.fs, leaf, kill_timeout)
        try:
            if leaf.is_symlink() or os.lstat(leaf).st_uid != os.getuid():
                raise PermissionError('Leaf is not an operator-owned directory')
            run._spawn(list(argv), env, cwd, stdin, stdout, stderr, start_timeout)
        except BaseException as exc:
            run._cleanup_after(exc)
            raise
        return run

    def adopt(self, name, kill_timeout=5.0):
        """Re-adopt a registered leaf after a restart: a handle that can kill it later.

        The pid is not this process's child, so its exit can never be seen here; the
        best close() can book is ``empty`` (S5: never terminated without a seen exit).
        As a context manager, that ``empty`` outcome is a finished cleanup. A leaf
        without the ownership marker written at creation is not ours and is refused.
        """
        leaf = self.leaf(name)
        if leaf.is_symlink() or not leaf.is_dir() or os.lstat(leaf).st_uid != os.getuid():
            raise PermissionError('Leaf is not an operator-owned directory')
        run = LeafRun(self.fs, leaf, kill_timeout)
        run.adopted = True
        try:
            owned = run.dirfd is not None and _marked_owned(run.dirfd)
        except OSError as exc:
            run._close_dirfd()
            raise PermissionError(f'Leaf ownership marker unreadable: {exc.errno}') from exc
        if run.dirfd is None or not owned:
            run._close_dirfd()
            raise PermissionError('Leaf has no recursion ownership marker')
        return run

    def sweep(self, keep, kill_timeout=5.0):
        """Kill leftover leaves this launcher created and the registry no longer lists.

        ``keep`` is required and must be every leaf name the registry still owns
        (runs to re-adopt and runs of any other launcher on this root). Those leaves
        are not touched; re-adopt them with adopt(). There is deliberately no
        kill-everything default: a daemon restart or upgrade must not end live runs
        (PRODUCT.md 055cc7a5 section 3.1, grill Q38 / O53).

        A directory is killed only when its name is a leaf id and the
        directory fd opened afterwards is still that inode and carries the
        ownership marker. The ``run-`` prefix alone is not ownership. A
        ``run-*`` name that is not a leaf id, any other name, a leaf id
        without the marker, a leaf id whose marker cannot be read, an opened
        inode that is not the one just classified, and a symlink are left in
        place and returned in ``reported``. They are not killed. A leaf whose
        ``cgroup.procs`` still lists an id is not removed. Exits of swept leaves
        are unseen, so they are never booked terminated.
        """
        if isinstance(keep, (str, bytes)):
            raise TypeError('keep must be a collection of leaf names, not a string')
        keep = set(keep)
        for name in keep:
            self.leaf(name)  # validate: a malformed name cannot match a leaf
        outcomes = {}
        reported = []

        def report(name, reason):
            reported.append((name, reason))
            log.warning('cgroup %s left in place during sweep: %s', name, reason)

        for leaf in sorted(self.path.iterdir()):
            if leaf.name in keep:
                continue
            try:
                info = os.lstat(leaf)
            except OSError:
                report(leaf.name, 'unreadable')
                continue
            if stat.S_ISLNK(info.st_mode):
                report(leaf.name, 'symlink')
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            # The run- prefix is not ownership. Only a leaf id can be ours,
            # and only together with the marker on the inode we just saw.
            if LEAF_NAME.fullmatch(leaf.name) is None:
                report(leaf.name, 'foreign-name')
                continue
            classified = (info.st_dev, info.st_ino)
            run = LeafRun(self.fs, leaf, kill_timeout)
            try:
                if run.dirfd is None:
                    report(leaf.name, 'unreadable')
                    continue
                # The name can be replaced between the classification lstat
                # and this open. fstat and the marker both go through the fd
                # (getxattr on the fd), and both must still describe the
                # classified inode. Otherwise leave it in place.
                try:
                    opened = os.fstat(run.dirfd)
                except OSError:
                    report(leaf.name, 'unreadable')
                    continue
                if ((opened.st_dev, opened.st_ino) != classified
                        or not stat.S_ISDIR(opened.st_mode)):
                    report(leaf.name, 'foreign-name')
                    continue
                try:
                    owned = _marked_owned(run.dirfd)
                except OSError as exc:
                    # ENODATA is unmarked, inside _marked_owned. Anything else
                    # (EOPNOTSUPP, EIO, ...) must stay visible. Do not kill a
                    # leaf whose marker could not be read.
                    report(leaf.name, f'marker-unreadable:{exc.errno}')
                    continue
                if not owned:
                    report(leaf.name, 'unmarked')
                    continue
                outcomes[leaf.name] = run.close()
            finally:
                # This handle is not kept for a later retry.
                run._close_dirfd()
        return SweepResult(outcomes, tuple(reported))


class LeafRun:
    def __init__(self, fs: Cgroup2, leaf: Path, kill_timeout: float):
        self.fs, self.leaf, self.kill_timeout = fs, leaf, kill_timeout
        self.proc = None
        self.pidfd = None
        self.outcome = None
        self.removing = self.removed = False
        self.lock = threading.Lock()
        self.dirfd = None
        self.identity = None
        self.adopted = False  # adopt() books empty; a launch must reach leaf_empty
        self._open_identity()

    def _open_identity(self):
        try:
            fd = os.open(self.leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError:
            return
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            return
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            return
        self.dirfd = fd
        self.identity = (info.st_dev, info.st_ino)

    def _same_cgroup(self):
        """The path still names the directory this handle opened."""
        return self.identity is not None and cgroup_identity(self.leaf) == self.identity

    def _close_dirfd(self):
        fd = self.dirfd
        self.dirfd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _foreign(self, populated=None):
        """A path whose cgroup is not the one this handle opened. Leave it in place."""
        self._close_dirfd()
        self.identity = None
        seen = self.proc is not None and self.proc.returncode is not None
        outcome = Outcome('unknown', self.proc.returncode if self.proc else None, populated,
                          seen, ('cgroup identity unknown or changed; left in place',))
        log.error('leaf %s: %s', self.leaf, outcome)
        return outcome

    def __del__(self):
        fd = getattr(self, 'dirfd', None)
        self.dirfd = None
        if fd is None:
            return
        try:
            os.close(fd)
        except Exception:
            pass

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def _spawn(self, argv, env, cwd, stdin, stdout, stderr, start_timeout):
        procs = self.fs.open_procs(self.leaf)
        read_end = write_end = -1
        try:
            read_end, write_end = os.pipe2(os.O_CLOEXEC)
            self.proc = subprocess.Popen(
                [sys.executable, '-I', '-S', '-c', TRAMPOLINE, str(procs), str(write_end), *argv],
                pass_fds=(procs, write_end), close_fds=True, start_new_session=True,
                env=env, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr)
            os.close(write_end)
            write_end = -1
            # Unreaped child: its pid cannot be reused, so this pidfd names exactly it.
            self.pidfd = os.pidfd_open(self.proc.pid)
            message = self._read_status(read_end, start_timeout)
        finally:
            for fd in (procs, read_end, write_end):
                if fd >= 0:
                    os.close(fd)
        if message:
            raise LaunchError(f'trampoline failed before exec: {message}')
        where = self.fs.member(self.proc.pid)
        if where != self.leaf:
            raise LaunchError(f'process is not in the leaf (seen in {where})')

    @staticmethod
    def _read_status(fd, timeout):
        """EOF without data means exec succeeded (the pipe closed on exec)."""
        data, deadline = b'', time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise LaunchError('trampoline did not report exec in time')
            chunk = os.read(fd, 4096)
            if not chunk:
                return data.decode(errors='replace')
            data += chunk

    def wait(self, timeout=None):
        """Wait for the main process. Descendants are handled by close()."""
        return self.proc.wait(timeout)

    def close(self) -> Outcome:
        """Kill the leaf and collect evidence. Never raises for kill errors.

        A final outcome (the leaf was removed) is cached and returned again. Any other
        outcome keeps the leaf and, while the main exit is unseen, the pidfd, so a
        later close() (or the startup sweep) retries the kill.
        """
        with self.lock:
            if self.outcome is None or not self.removed:
                self.outcome = self._terminate()
            return self.outcome

    def _held_inode_removed(self):
        """True only when the inode this handle opened is proven gone.

        The path is not consulted as proof. A missing path can be a directory
        that was swapped in and deleted while this inode was renamed aside.
        """
        if self.dirfd is None or self.identity is None:
            return False
        parent_fd = None
        try:
            try:
                parent_fd = os.open(self.leaf.parent,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            except OSError:
                parent_fd = None
            return _held_directory_removed(parent_fd, self.leaf.name, self.dirfd, self.identity)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

    def _terminate(self):
        if self.removing and not self.removed:
            if self._held_inode_removed():
                self.removed = True  # interrupted after the inode was removed
            elif not self._same_cgroup():
                # The path is missing or names another directory. That does
                # not prove the held inode is gone, and it must not be dropped.
                seen = self.proc is not None and self.proc.returncode is not None
                outcome = Outcome('unknown', self.proc.returncode if self.proc else None,
                                  None, seen, ('cgroup identity still present after rmdir',))
                log.error('leaf %s: %s', self.leaf, outcome)
                return outcome
        if self.removed:  # the leaf was empty and is gone: rebuild the outcome
            self._close_dirfd()
            seen = self.proc is not None and self.proc.returncode is not None
            return Outcome('leaf_empty' if seen else 'empty',
                           self.proc.returncode if self.proc else None, False, seen)
        # A retry may run after the path was reused. Kill and rmdir only the inode
        # this handle opened; an unknown or replaced identity is left in place.
        if not self._same_cgroup():
            return self._foreign()
        errors = {}  # distinct messages, first-seen order; the loop retries
        try:
            budget = max(0.0, float(self.kill_timeout))
        except (TypeError, ValueError):
            budget = 0.0
        deadline = time.monotonic() + budget
        populated = None
        attempted = False
        while True:
            # One attempt even when the budget is already spent; further attempts
            # stop at the deadline so a slow exit cannot hold the window open.
            if attempted and time.monotonic() >= deadline:
                break
            attempted = True
            try:
                self.fs.kill(self.leaf, deadline=deadline, dirfd=self.dirfd)
            except OSError as exc:
                errors.setdefault(f'cgroup.kill: {exc!r}')
            if not self._same_cgroup():
                return self._foreign(populated)
            if self.pidfd is not None and self.proc is not None and self.proc.returncode is None:
                try:
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    errors.setdefault(f'pidfd_send_signal: {exc!r}')
            # poll() is WNOHANG and does not wait on another thread's blocking wait.
            if self.proc is not None:
                self.proc.poll()
            try:
                populated = self.fs.populated(self.leaf, deadline=deadline, dirfd=self.dirfd)
            except OSError:
                populated = None
            seen = self.proc is not None and self.proc.returncode is not None
            if populated is False and (seen or self.proc is None):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
        seen = self.proc is not None and self.proc.returncode is not None
        if seen and self.pidfd is not None:
            # Close only once the exit is seen: until then the pidfd is the kill handle.
            # Detach first, so an interrupt leaks the fd rather than closing it twice.
            fd, self.pidfd = self.pidfd, None
            os.close(fd)
        if not self._same_cgroup():
            return self._foreign(populated)
        if populated is False and seen:
            state = 'leaf_empty'  # not 'terminated': a same-uid escapee would be unseen
        elif populated is False and self.proc is None:
            state = 'empty'  # sweep, or no process started: empty, but no exit was seen
        elif populated is True:
            state = 'running'
        else:
            state = 'unknown'  # includes populated 0 with the main exit unseen
        if state in ('leaf_empty', 'empty'):
            self.removing = True
            try:
                self.fs.remove(self.leaf, deadline=deadline, identity=self.identity,
                               dirfd=self.dirfd)
                self.removed = True
            except OSError as exc:
                errors.setdefault(f'rmdir: {exc!r}')
                # Identity not confirmed gone: do not book empty/removed.
                if not self.removed and exc.strerror and 'identity' in exc.strerror:
                    state = 'unknown'
            if self.removed:
                self._close_dirfd()
        outcome = Outcome(state, self.proc.returncode if self.proc else None, populated,
                          seen, tuple(errors))
        if state not in ('leaf_empty', 'empty') or errors:
            log.error('leaf %s: %s', self.leaf, outcome)
        return outcome

    def _close_retrying(self):
        """close(), retried when an interrupt lands inside it (C2-10: finally can be
        interrupted). Returns (outcome or None, interrupts). If every attempt is
        interrupted the leaf stays on disk for the startup sweep."""
        interrupts = []
        for _ in range(CLEANUP_ATTEMPTS):
            try:
                return self.close(), interrupts
            except BaseException as cleanup:
                interrupts.append(cleanup)
                log.exception('leaf %s cleanup interrupted', self.leaf)
        return None, interrupts

    def _cleanup_after(self, exc):
        """Clean up after a failure without hiding the original exception."""
        outcome, interrupts = self._close_retrying()
        for cleanup in interrupts:
            exc.add_note(f'leaf cleanup raised: {cleanup!r}')
        if outcome is None:
            exc.add_note(f'leaf cleanup abandoned after {CLEANUP_ATTEMPTS} interrupted '
                         'attempts; leaf kept for the startup sweep')
        elif outcome.state not in ('leaf_empty', 'empty') or outcome.errors:
            exc.add_note(f'leaf cleanup incomplete: {outcome}')

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if exc is not None:
            self._cleanup_after(exc)
            return False
        try:
            outcome = self.close()
        except BaseException as interrupt:
            log.exception('leaf %s cleanup interrupted', self.leaf)
            self._cleanup_after(interrupt)  # retries; the interrupt still propagates
            raise
        if not self._exit_complete(outcome):
            raise CleanupIncomplete(outcome)
        return False

    def _exit_complete(self, outcome):
        """True when cleanup finished for this kind of handle.

        A launch is finished only at ``leaf_empty`` (the main exit was seen).
        An adopted leaf cannot see that exit, so ``empty`` with no error is
        finished. Anything else, including ``empty`` plus an rmdir error, is not.
        """
        if outcome.errors:
            return False
        if outcome.state == 'leaf_empty':
            return True
        return self.adopted and outcome.state == 'empty'
