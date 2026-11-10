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

LEAF_NAME = re.compile(r'run-[a-zA-Z0-9_-]{1,64}')
OWNED_PREFIX = 'run-'  # recursion-owned names; sweep never leaves this prefix
CLEANUP_ATTEMPTS = 5  # bounded: an interrupt storm leaves the leaf to the sweep
MAX_EBUSY_ATTEMPTS = 32  # dying-task rmdir retries, also cut by the cleanup deadline
_READ_CAP = 1 << 20
_MAX_CGROUP_DEPTH = 64


def cgroup_identity(path):
    """``(st_dev, st_ino)`` of a real directory, or None when the path is not one."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def _owned_dirname(name):
    """A single path component under the recursion-owned ``run-`` prefix."""
    return (isinstance(name, str) and name.startswith(OWNED_PREFIX)
            and name not in ('.', '..') and '/' not in name and '\x00' not in name)


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


class Cgroup2:
    """cgroup v2 filesystem operations. Tests substitute a fake."""
    def __init__(self, mount=Path('/sys/fs/cgroup')):
        self.mount = Path(os.path.realpath(mount))

    def make_leaf(self, leaf: Path):
        os.mkdir(leaf, 0o755)  # exclusive: an existing leaf is never reused

    def open_procs(self, leaf: Path) -> int:
        return os.open(leaf / 'cgroup.procs', os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)

    def kill(self, leaf: Path, deadline=None):
        # O_NONBLOCK: a fifo or a stuck node must not hold the cleanup window open.
        fd = os.open(leaf / 'cgroup.kill',
                     os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not _wait_fd(fd, True, deadline):
                raise OSError(errno.ETIMEDOUT, 'cgroup.kill not ready before deadline')
            os.write(fd, b'1')
        finally:
            os.close(fd)

    def _read_text(self, path, deadline):
        """Read a cgroup file without blocking past ``deadline``. None if it cannot."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            chunks, size = [], 0
            while size <= _READ_CAP:
                if not _wait_fd(fd, False, deadline):
                    return None
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    return None
                if not chunk:
                    return b''.join(chunks).decode(errors='replace')
                chunks.append(chunk)
                size += len(chunk)
            return None
        finally:
            os.close(fd)

    def populated(self, leaf: Path, deadline=None) -> bool | None:
        text = self._read_text(Path(leaf) / 'cgroup.events', deadline)
        if not text:
            return None
        try:
            value = dict(line.split() for line in text.splitlines() if line.strip()).get('populated')
        except ValueError:
            return None
        return {'0': False, '1': True}.get(value)

    def _child_cgroups(self, directory, deadline):
        """Child directories, or None when the listing is unsafe or incomplete."""
        children = []
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if time.monotonic() >= deadline:
                        return None
                    if entry.is_symlink():
                        return None  # do not follow or delete through a link
                    if entry.is_dir(follow_symlinks=False):
                        children.append(Path(entry.path))
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
                token = line.strip()
                if not token:
                    continue
                if not token.isdigit():
                    return False
                return False
        return True

    def _confirmed_empty(self, directory, deadline):
        children = self._child_cgroups(directory, deadline)
        if children is None or children:
            return False
        return self._procs_empty(directory, deadline)

    def _identity_ok(self, directory, identity):
        if identity is None:
            return True
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

    def remove(self, leaf: Path, deadline=None, identity=None):
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
        """
        if identity is None or cgroup_identity(leaf) != identity:
            raise OSError(errno.EBUSY, 'cgroup identity unknown or changed')
        if deadline is None:
            deadline = time.monotonic() + 0.25
        self._remove_dir(Path(leaf), deadline, identity, 0)

    def _remove_dir(self, directory, deadline, identity, depth):
        if depth > _MAX_CGROUP_DEPTH:
            raise OSError(errno.EBUSY, 'cgroup tree too deep')
        if not self._identity_ok(directory, identity):
            raise OSError(errno.EBUSY, 'cgroup identity changed')
        children = self._child_cgroups(directory, deadline)
        if children is None:
            raise OSError(errno.EBUSY, 'cgroup not confirmed empty')
        for child in children:
            if not self._identity_ok(directory, identity):
                raise OSError(errno.EBUSY, 'cgroup identity changed')
            self._remove_dir(child, deadline, None, depth + 1)
        self._rmdir_when_empty(directory, deadline, identity)

    def _rmdir_when_empty(self, directory, deadline, identity):
        attempts = 0
        while True:
            if not self._identity_ok(directory, identity):
                raise OSError(errno.EBUSY, 'cgroup identity changed')
            if not self._confirmed_empty(directory, deadline):
                raise OSError(errno.EBUSY, 'cgroup not empty')
            try:
                self._rmdir(directory)
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                if exc.errno != errno.EBUSY:
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

    def _rmdir(self, directory):
        os.rmdir(directory)


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
        """
        leaf = self.leaf(name)
        if leaf.is_symlink() or not leaf.is_dir() or os.lstat(leaf).st_uid != os.getuid():
            raise PermissionError('Leaf is not an operator-owned directory')
        return LeafRun(self.fs, leaf, kill_timeout)

    def sweep(self, keep, kill_timeout=5.0):
        """Kill leftover leaves that the durable run registry does not list.

        ``keep`` is required and must be every leaf name the registry still owns
        (runs to re-adopt and runs of any other launcher on this root). Those leaves
        are not touched; re-adopt them with adopt(). There is deliberately no
        kill-everything default: a daemon restart or upgrade must not end live runs
        (PRODUCT.md 055cc7a5 section 3.1, grill Q38 / O53). Names that are not valid
        leaf ids are still swept when they use the recursion-owned ``run-`` prefix;
        directories outside that prefix, and symlinks, are left alone. Exits of
        swept leaves are unseen, so they are never booked terminated.
        """
        if isinstance(keep, (str, bytes)):
            raise TypeError('keep must be a collection of leaf names, not a string')
        keep = set(keep)
        for name in keep:
            self.leaf(name)  # validate: a malformed name cannot match a leaf
        outcomes = {}
        for leaf in sorted(self.path.iterdir()):
            # Valid leaf ids are a subset of the prefix. Unregistered names that
            # still start with run- are leftovers too; anything else is foreign.
            if leaf.name in keep or not _owned_dirname(leaf.name) or leaf.is_symlink():
                continue
            try:
                info = os.lstat(leaf)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            run = LeafRun(self.fs, leaf, kill_timeout)
            try:
                outcomes[leaf.name] = run.close()
            finally:
                # This handle is not kept for a later retry.
                run._close_dirfd()
        return outcomes


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

    def _terminate(self):
        if self.removing and not os.path.lexists(self.leaf):
            self.removed = True  # interrupted inside or after the rmdir
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
                self.fs.kill(self.leaf, deadline=deadline)
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
                populated = self.fs.populated(self.leaf, deadline=deadline)
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
                self.fs.remove(self.leaf, deadline=deadline, identity=self.identity)
                self.removed = True
            except OSError as exc:
                errors.setdefault(f'rmdir: {exc!r}')
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
        if outcome.state != 'leaf_empty' or outcome.errors:
            raise CleanupIncomplete(outcome)
        return False
